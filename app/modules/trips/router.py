import redis
import calendar
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import List, Union
from sqlalchemy.orm import selectinload

from app.core.database import get_session, get_redis
from app.core.models import (
    Trip,
    TripCreate,
    TripOffer,
    TripOfferPublic,
    TripReadUser,
    TripSafe,
    Driver,
    User,
    PaymentMethodSelect,
    DriverAcceptanceRequest,
    DriverPaymentRequest,
    OTPVerificationRequest,
    TripSkipRequest,
    TripEndRequest,
    BillPaymentRequest,
    MarkBillPaidRequest,
    FareEstimateRequest,
    TripAttendance,
    TripBill,
    TripSettlement,
    PaymentTransaction,
)
from app.core.security import get_current_user
from app.modules.trips.allocation import (
    rank_drivers,
    create_offers_for_tier,
    process_tier_escalation,
    attempt_trip_escalation,
)
from app.modules.trips.otp_service import OTPService
from app.modules.trips.payment_service import PaymentService, get_driver_acceptance_fee
from app.modules.trips.trip_service import TripService
from app.modules.trips.billing_service import BillingService
from app.modules.trips.pricing_calculator import (
    calculate_fare,
    validate_pricing_inputs,
)
from app.utils.time_utils import now_ist, today_ist, to_ist_naive
from app.utils.notifications import send_push_notification

router = APIRouter(prefix="/trips", tags=["Trips"])

TIER_SIZE = 3


@router.post("/estimate-fare")
def estimate_fare_for_booking(
    fare_req: FareEstimateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    if not fare_req.vehicle_type:
        raise HTTPException(400, "vehicle_type is required")

    ok, err = validate_pricing_inputs(
        hiring_type=fare_req.hiring_type,
        distance_km=fare_req.distance_km,
        start_lat=fare_req.start_lat,
        start_lng=fare_req.start_lng,
        end_lat=fare_req.end_lat,
        end_lng=fare_req.end_lng,
        end_location=fare_req.end_location,
    )
    if not ok:
        raise HTTPException(400, err)

    return calculate_fare(
        session,
        redis_client,
        hiring_type=fare_req.hiring_type,
        vehicle_type=fare_req.vehicle_type,
        shift_details=fare_req.shift_details,
        start_date=fare_req.start_date,
        end_date=fare_req.end_date,
        months=fare_req.months,
        selected_days=fare_req.selected_days,
        start_location=fare_req.start_location,
        end_location=fare_req.end_location,
        start_lat=fare_req.start_lat,
        start_lng=fare_req.start_lng,
        end_lat=fare_req.end_lat,
        end_lng=fare_req.end_lng,
        distance_km=fare_req.distance_km,
        booking_time=to_ist_naive(fare_req.booking_time) or now_ist(),
    )


@router.post("/book-request", response_model=TripSafe)
def create_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    trip_in: TripCreate,
):
    if not trip_in.vehicle_type:
        raise HTTPException(400, "Vehicle type is required.")

    BLOCKING_STATES = (
        "searching",
        "accepted_pending_payment",
        "payment_in_progress",
        "active_pending_otp",
        "active",
        "ongoing",
        "paused",
    )
    in_flight = session.exec(
        select(Trip).where(
            Trip.user_id == current_user.id,
            Trip.status.in_(BLOCKING_STATES),
        )
    ).first()
    if in_flight:
        raise HTTPException(
            409,
            f"You already have an active trip (id={in_flight.id}, status={in_flight.status}). "
            "Complete or cancel it before booking another.",
        )

    payment_service = PaymentService(None)
    if payment_service.user_has_unpaid_bills(session, current_user.id):
        raise HTTPException(
            409,
            "You have unpaid bills from previous trips. Please settle them before booking a new trip.",
        )

    trip_data = trip_in.model_dump()

    if trip_data.get("hiring_type") == "Monthly":
        if not trip_data.get("start_date") or not trip_data.get("end_date"):
            today = today_ist()
            start_date = today.replace(day=1)
            months_count = trip_data.get("months") or 1

            end_month = start_date.month + months_count - 1
            end_year = start_date.year + (end_month // 12)
            end_month = (end_month % 12) + 1

            _, last_day = calendar.monthrange(end_year, end_month)
            end_date = date(end_year, end_month, last_day)

            trip_data["start_date"] = start_date
            trip_data["end_date"] = end_date
    else:
        if not trip_data.get("start_date") or not trip_data.get("end_date"):
            raise HTTPException(
                400, "start_date and end_date are required for this hiring type."
            )

    ok, err = validate_pricing_inputs(
        hiring_type=trip_data.get("hiring_type"),
        distance_km=trip_data.get("distance_km"),
        start_lat=trip_data.get("start_lat"),
        start_lng=trip_data.get("start_lng"),
        end_lat=trip_data.get("end_lat"),
        end_lng=trip_data.get("end_lng"),
        end_location=trip_data.get("end_location"),
    )
    if not ok:
        raise HTTPException(400, err)

    fare_dict = calculate_fare(
        session,
        redis_client,
        hiring_type=trip_data.get("hiring_type"),
        vehicle_type=trip_data.get("vehicle_type"),
        shift_details=trip_data.get("shift_details"),
        start_date=trip_data.get("start_date"),
        end_date=trip_data.get("end_date"),
        months=trip_data.get("months"),
        selected_days=trip_data.get("selected_days"),
        start_location=trip_data.get("start_location"),
        end_location=trip_data.get("end_location"),
        start_lat=trip_data.get("start_lat"),
        start_lng=trip_data.get("start_lng"),
        end_lat=trip_data.get("end_lat"),
        end_lng=trip_data.get("end_lng"),
        distance_km=trip_data.get("distance_km"),
        booking_time=now_ist(),
    )
    trip_data["fare"] = fare_dict["total"]
    trip_data["fare_breakdown"] = fare_dict

    if trip_data.get("distance_km") is None and fare_dict["meta"].get("distance_km"):
        trip_data["distance_km"] = fare_dict["meta"]["distance_km"]

    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"

    db_trip = Trip.model_validate(trip_data)

    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    ranked_drivers = rank_drivers(session, trip_in.vehicle_type)

    if not ranked_drivers:
        db_trip.status = "no_drivers_found"
        session.add(db_trip)
        session.commit()
        return db_trip

    tier_1_drivers = ranked_drivers[:TIER_SIZE]
    create_offers_for_tier(session, db_trip.id, tier_1_drivers, tier=1)

    return db_trip


@router.get("/my-bookings", response_model=List[Union[TripReadUser, TripSafe]])
def get_my_bookings(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == "driver":
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        if not driver:
            return []
        return session.exec(select(Trip).where(Trip.driver_id == driver.id)).all()

    elif current_user.role == "user":
        DRIVER_HIDDEN_STATES = {
            "searching",
            "no_drivers_found",
            "accepted_pending_payment",
            "payment_in_progress",
            "payment_failed",
            "rejected",
        }
        statement = (
            select(Trip)
            .where(Trip.user_id == current_user.id)
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.driver))
        )
        trips = session.exec(statement).all()

        for t in trips:
            if t.status in DRIVER_HIDDEN_STATES:
                t.driver = None
                t.driver_id = None
        return trips
    else:
        return []


@router.post("/{trip_id}/cancel")
def cancel_trip(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False
    driver = None
    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = bool(driver and trip.driver_id == driver.id)
    if not (is_user or is_driver):
        raise HTTPException(403, "Not authorized to cancel this trip")

    if trip.status in (
        "completed",
        "cancelled",
        "cancelled_by_user",
        "cancelled_by_driver",
        "billed",
        "settled",
    ):
        raise HTTPException(400, "Trip cannot be cancelled in its current state")

    trip_service = TripService()
    can_cancel, reason = trip_service.can_cancel_trip(
        session,
        trip_id,
        user_id=str(current_user.id) if is_user else None,
        driver_id=driver.id if is_driver else None,
    )
    if not can_cancel:
        raise HTTPException(400, reason or "Trip cannot be cancelled")

    payment_service = PaymentService(redis_client)
    if payment_service.trip_has_unpaid_bills(session, trip_id):
        raise HTTPException(
            400,
            "Cannot cancel trip with unpaid daily bills. Settle outstanding bills first.",
        )
    refund_amount, refund_err = payment_service.calculate_refund_amount(
        session,
        trip_id,
        cancellation_reason=("user_cancel" if is_user else "driver_cancel"),
    )
    if refund_err:
        raise HTTPException(400, refund_err)
    if refund_amount and refund_amount > 0:
        ok, err = payment_service.process_refund(
            session,
            trip_id,
            refund_amount,
            reason=("Cancelled by user" if is_user else "Cancelled by driver"),
        )
        if not ok:
            raise HTTPException(400, err or "Refund failed")

    new_state = "cancelled_by_user" if is_user else "cancelled_by_driver"
    trip.status = new_state
    session.add(trip)

    offers = session.exec(select(TripOffer).where(TripOffer.trip_id == trip.id)).all()
    for offer in offers:
        if offer.status == "pending":
            session.delete(offer)

    session.commit()

    return {
        "message": "Trip cancelled successfully",
        "refund_amount": refund_amount or 0.0,
        "trip_status": new_state,
    }


@router.get("/driver/offers", response_model=List[TripOfferPublic])
def get_driver_offers(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    statement = (
        select(TripOffer)
        .where(TripOffer.driver_id == driver.id)
        .where(TripOffer.status == "pending")
        .options(selectinload(TripOffer.trip))
    )

    offers = session.exec(statement).all()
    return offers


@router.post("/driver/accept-offer/{offer_id}", deprecated=True)
def accept_trip_offer(
    offer_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can accept trips")

    offer = session.get(TripOffer, offer_id)
    if not offer or offer.driver_id != driver.id:
        raise HTTPException(404, "Offer not found or not authorized")

    if offer.status != "pending":
        raise HTTPException(400, "Offer is no longer valid")

    trip = session.get(Trip, offer.trip_id)
    if trip.status != "searching":
        raise HTTPException(400, "Trip has already been taken by another driver")

    trip.driver_id = driver.id
    trip.status = "accepted"

    offer.status = "accepted"
    session.add(trip)
    session.add(offer)

    other_offers = session.exec(
        select(TripOffer).where(TripOffer.trip_id == trip.id)
    ).all()
    for o in other_offers:
        if o.id != offer.id:
            session.delete(o)

    session.commit()

    if redis_client:
        redis_client.delete(f"driver_{driver.id}")

    return {
        "message": "Trip accepted. Other offers have been removed.",
        "trip_id": trip.id,
    }


@router.post("/driver/reject-offer/{offer_id}")
def reject_trip_offer(
    offer_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Not authorized")

    offer = session.get(TripOffer, offer_id)
    if not offer or offer.driver_id != driver.id:
        raise HTTPException(404, "Offer not found")

    offer.status = "rejected"
    session.add(offer)
    session.commit()

    trip = session.get(Trip, offer.trip_id)
    if trip and trip.status == "searching":
        escalated = attempt_trip_escalation(session, trip)
        if escalated:
            session.commit()

    return {"message": "Offer rejected"}


@router.post("/check-escalation")
def check_and_escalate_tiers(session: Session = Depends(get_session)):
    count = process_tier_escalation(session)
    return {"message": f"Escalated {count} trips."}


@router.post("/{trip_id}/select-payment-method")
def select_payment_method(
    trip_id: int,
    payment_req: PaymentMethodSelect,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Not authorized")

    if trip.payment_method:
        raise HTTPException(400, "Payment method already selected")

    if payment_req.payment_method not in ["trip_day", "advance_20", "full_payment"]:
        raise HTTPException(400, "Invalid payment method")

    trip.payment_method = payment_req.payment_method
    session.add(trip)
    session.commit()

    return {"message": f"Payment method {payment_req.payment_method} selected"}


@router.post("/driver/{trip_id}/accept-and-pay")
def driver_accept_and_initiate_payment(
    trip_id: int,
    accept_req: DriverAcceptanceRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can perform this action")

    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_id and trip.driver_id != driver.id:
        raise HTTPException(400, "Driver not matched to this trip")

    if accept_req.action == "reject":
        offer = session.exec(
            select(TripOffer).where(
                TripOffer.trip_id == trip_id,
                TripOffer.driver_id == driver.id,
            )
        ).first()

        if offer:
            offer.status = "rejected"
            session.add(offer)

        refund_amount = 0.0
        if trip.driver_payment_status == "paid":
            payment_service = PaymentService(redis_client)
            ok, err = payment_service.refund_driver_acceptance_fee(
                session, trip_id, driver.id, reason="Driver rejected after payment"
            )
            if not ok:
                raise HTTPException(400, err or "Refund failed")
            refund_amount = trip.driver_payment_amount or get_driver_acceptance_fee(
                session, redis_client
            )
            trip.driver_payment_status = "unpaid"
            trip.driver_payment_amount = None

        trip.driver_id = None
        trip.driver_accepted_at = None
        if trip.status in ("accepted_pending_payment", "payment_in_progress"):
            trip.status = "searching"
        session.add(trip)
        session.commit()

        if trip.status == "searching":
            attempt_trip_escalation(session, trip)
            session.commit()

        return {
            "message": "Trip rejected",
            "refund_amount": refund_amount,
        }

    elif accept_req.action == "accept":
        trip.driver_id = driver.id
        trip.status = "accepted_pending_payment"
        trip.driver_accepted_at = now_ist()

        redis_key = f"driver_payment_timer:{trip_id}:{driver.id}"
        redis_client.setex(redis_key, 1800, "pending")

        session.add(trip)
        session.commit()

        return {
            "message": "Trip accepted. Payment required.",
            "trip_id": trip_id,
            "payment_required": get_driver_acceptance_fee(session, redis_client),
            "timer_seconds": 1800,
            "payment_deadline": (now_ist() + timedelta(minutes=30)).isoformat(),
        }

    else:
        raise HTTPException(400, "Invalid action. Use 'accept' or 'reject'")


@router.post("/driver/{trip_id}/process-payment")
def driver_process_payment(
    trip_id: int,
    payment_req: DriverPaymentRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can perform this action")

    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_id != driver.id:
        raise HTTPException(403, "Not authorized")

    if trip.status != "accepted_pending_payment":
        raise HTTPException(
            400, f"Trip status is {trip.status}, cannot process payment now"
        )

    payment_service = PaymentService(redis_client)
    success, error = payment_service.driver_accept_payment(session, trip_id, driver.id)

    if not success:
        raise HTTPException(400, f"Payment failed: {error}")

    trip_service = TripService()

    # Generate schedules upon successful driver payment and fast track to active_pending_otp
    if trip.start_date:
        duration_hours = (
            trip_service.get_trip_duration_hours(trip.shift_details)
            or trip.trip_duration_hours
            or 8
        )

        parsed_time = trip_service.get_trip_start_time(
            trip.shift_details, trip.start_date
        )

        effective_start_dt = (
            parsed_time
            or trip.scheduled_start_time
            or datetime.combine(trip.start_date, datetime.min.time())
        )

        if parsed_time and not trip.scheduled_start_time:
            trip.scheduled_start_time = parsed_time
            trip.scheduled_end_time = parsed_time + timedelta(hours=duration_hours)
            trip.trip_duration_hours = duration_hours

        end_date_for_attendance = trip.end_date or trip.start_date
        att_ok, att_err = trip_service.create_trip_attendance_records(
            session,
            trip_id,
            trip.start_date,
            end_date_for_attendance,
            effective_start_dt,
            duration_hours,
            selected_days=trip.selected_days,
        )
        if not att_ok:
            raise HTTPException(400, att_err)

    success, error = trip_service.transition_trip_state(
        session, trip_id, "active_pending_otp", validate=True
    )

    if not success:
        raise HTTPException(400, f"Status update failed: {error}")

    redis_key = f"driver_payment_timer:{trip_id}:{driver.id}"
    redis_client.delete(redis_key)

    return {
        "message": "Driver payment successful. Trip is ready for OTP verification.",
        "trip_id": trip_id,
        "trip_status": trip.status,
    }


@router.post("/{trip_id}/request-otp")
def request_otp_for_trip(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can fetch the OTP")

    if trip.status not in ("active_pending_otp", "ongoing"):
        raise HTTPException(
            400, f"Trip status is {trip.status}, OTP cannot be generated"
        )

    if not trip.payment_method:
        raise HTTPException(
            400, "Please select a payment method before requesting OTP."
        )

    if not trip.scheduled_start_time:
        raise HTTPException(400, "Trip schedule not set")

    if trip.is_payment_blocked:
        raise HTTPException(
            400,
            "Trip is paused — settle outstanding daily bills before requesting today's OTP.",
        )

    attendances_raw = session.exec(
        select(TripAttendance)
        .where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
        .order_by(TripAttendance.trip_date)
    ).all()

    attendances = [att for att in attendances_raw if not att.user_otp_verified]

    valid_attendance = None
    now = now_ist()
    for att in attendances:
        # Generous 12-hour expiry window matching OTPService
        expiry_time = att.scheduled_start + timedelta(hours=12)
        if now > expiry_time:
            att.status = "skipped_by_system"
            att.skip_reason = "Shift window expired without verification"
            att.marked_by = "system"
            session.add(att)
            session.commit()
        else:
            valid_attendance = att
            break

    if not valid_attendance:
        raise HTTPException(
            400,
            "No pending scheduled shifts found for this trip. The trip may be completed or paused.",
        )

    attendance = valid_attendance

    time_until_start = attendance.scheduled_start - now
    if time_until_start > timedelta(minutes=30):
        raise HTTPException(
            400,
            f"OTP can only be requested within 30 minutes of the scheduled trip start time ({attendance.scheduled_start.strftime('%I:%M %p %d-%b')}).",
        )

    trip_day = attendance.trip_date
    trip_start_for_day = attendance.scheduled_start

    otp_service = OTPService(redis_client)
    otp, error = otp_service.generate_otp(
        session, trip_id, trip_start_for_day, trip_date=trip_day
    )
    if error:
        raise HTTPException(400, error)

    try:
        send_push_notification(
            session=session,
            user_ids=[trip.user_id],
            title="Trip OTP Ready",
            body=f"Your trip OTP is ready. Open the app to view it and share with your driver to start trip #{trip_id}.",
            data={"type": "trip_otp", "trip_id": trip_id},
        )
    except Exception:
        pass

    expiry_time = otp_service.get_otp_expiry_time(session, trip_id, trip_day)
    validity_start = trip_start_for_day - timedelta(minutes=30)

    return {
        "message": "OTP generated. Share this with your driver to start the trip.",
        "otp": otp,
        "trip_date": trip_day.isoformat(),
        "validity_start": validity_start.isoformat(),
        "validity_end": expiry_time.isoformat() if expiry_time else None,
        "expires_in_seconds": int((expiry_time - now_ist()).total_seconds())
        if expiry_time
        else 0,
    }


@router.post("/{trip_id}/verify-otp")
def verify_otp_for_trip(
    trip_id: int,
    otp_req: OTPVerificationRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only the assigned driver can verify the OTP")

    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_id != driver.id:
        raise HTTPException(403, "Driver not assigned to this trip")

    if trip.status != "active_pending_otp":
        raise HTTPException(
            400, f"Trip status is {trip.status}, OTP verification not allowed"
        )

    attendances_raw = session.exec(
        select(TripAttendance)
        .where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
        .order_by(TripAttendance.trip_date)
    ).all()

    attendances = [att for att in attendances_raw if not att.user_otp_verified]

    valid_attendance = None
    now = now_ist()
    for att in attendances:
        expiry_time = att.scheduled_start + timedelta(hours=12)
        if now > expiry_time:
            att.status = "skipped_by_system"
            att.skip_reason = "Shift window expired without verification"
            att.marked_by = "system"
            session.add(att)
            session.commit()
        else:
            valid_attendance = att
            break

    if not valid_attendance:
        raise HTTPException(400, "No pending scheduled shifts found for this trip.")

    shift_date = valid_attendance.trip_date

    otp_service = OTPService(redis_client)
    is_valid, error = otp_service.verify_otp(
        session, trip_id, driver.id, otp_req.otp, trip_date=shift_date
    )
    if not is_valid:
        raise HTTPException(400, error)

    trip_service = TripService()
    trip_service.transition_trip_state(session, trip_id, "active", validate=False)
    trip_service.transition_trip_state(session, trip_id, "ongoing", validate=False)

    trip.actual_start_time = now_ist()

    valid_attendance.user_otp_verified = True
    valid_attendance.driver_otp_verified = True
    valid_attendance.actual_start = trip.actual_start_time
    session.add(valid_attendance)
    session.add(trip)
    session.commit()

    try:
        billing_service = BillingService()
        billing_service.generate_daily_bill(session, trip_id, shift_date)
    except Exception:
        pass

    try:
        send_push_notification(
            session=session,
            user_ids=[trip.user_id],
            title="Trip started",
            body=f"Your driver verified the OTP. Trip #{trip_id} is now in progress.",
            data={"type": "trip_started", "trip_id": trip_id},
        )
    except Exception:
        pass

    return {
        "message": "OTP verified. Trip is now ongoing!",
        "trip_id": trip_id,
        "trip_status": "ongoing",
    }


@router.post("/{trip_id}/skip-day")
def skip_trip_day(
    trip_id: int,
    skip_req: TripSkipRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    marked_by = "user"

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        if not driver or trip.driver_id != driver.id:
            raise HTTPException(403, "Not authorized")
        marked_by = "driver"

    trip_service = TripService()
    success, error = trip_service.mark_trip_day_absent(
        session, trip_id, skip_req.trip_date, skip_req.reason, marked_by
    )

    if not success:
        raise HTTPException(400, error)

    return {
        "message": f"Day {skip_req.trip_date} marked as absent by {marked_by}",
        "trip_id": trip_id,
        "trip_date": skip_req.trip_date.isoformat(),
    }


@router.post("/{trip_id}/end-trip")
def driver_end_trip(
    trip_id: int,
    end_req: TripEndRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can end trips")

    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_id != driver.id:
        raise HTTPException(403, "Not authorized")

    if trip.status != "ongoing":
        raise HTTPException(400, f"Trip status is {trip.status}, cannot end now")

    active_attendances_raw = session.exec(
        select(TripAttendance)
        .where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
        .order_by(TripAttendance.trip_date.desc())
    ).all()

    active_attendance = next(
        (att for att in active_attendances_raw if att.user_otp_verified), None
    )

    if not active_attendance:
        raise HTTPException(400, "Could not find the active shift to end.")

    shift_date = active_attendance.trip_date

    next_attendances_raw = session.exec(
        select(TripAttendance)
        .where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.trip_date > shift_date,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
        .order_by(TripAttendance.trip_date)
    ).all()

    next_attendance = next(
        (att for att in next_attendances_raw if not att.user_otp_verified), None
    )
    has_future_shifts = next_attendance is not None

    trip_service = TripService()
    next_state = "active_pending_otp" if has_future_shifts else "completed"
    success, error = trip_service.transition_trip_state(
        session, trip_id, next_state, validate=True
    )
    if not success:
        raise HTTPException(400, error)

    if not has_future_shifts:
        trip.actual_end_time = to_ist_naive(end_req.actual_end_time) or now_ist()
    else:
        trip.scheduled_start_time = next_attendance.scheduled_start
        trip.scheduled_end_time = next_attendance.scheduled_end

    session.add(trip)
    session.commit()

    trip_service.mark_trip_day_present(session, trip_id, shift_date)

    billing_service = BillingService()
    bill_success, bill_id, bill_error = billing_service.generate_daily_bill(
        session, trip_id, shift_date
    )

    if not bill_success and bill_id:
        bill_success = True

    if bill_success and bill_id:
        bill = session.get(TripBill, bill_id)
        amount = bill.amount_due if bill else 0.0
        try:
            if amount > 0:
                body = f"Trip #{trip_id} ended. Amount due: ₹{amount:.2f}."
            else:
                body = f"Trip #{trip_id} ended. Bill already paid."

            send_push_notification(
                session=session,
                user_ids=[trip.user_id],
                title="Trip ended — bill ready",
                body=body,
                data={
                    "type": "bill_generated",
                    "trip_id": trip_id,
                    "bill_id": bill_id,
                    "amount": amount,
                },
            )
        except Exception:
            pass

    return {
        "message": "Trip ended successfully",
        "trip_id": trip_id,
        "actual_end_time": trip.actual_end_time.isoformat()
        if trip.actual_end_time
        else None,
        "trip_status": trip.status,
        "more_days_remaining": has_future_shifts,
        "bill_generated": bill_success,
        "bill_id": bill_id,
    }


@router.get("/{trip_id}/bill/{bill_id}")
def get_trip_bill(
    trip_id: int,
    bill_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = driver and trip.driver_id == driver.id

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    bill_row = session.get(TripBill, bill_id)
    if not bill_row or bill_row.trip_id != trip_id:
        raise HTTPException(404, "Bill not found")

    billing_service = BillingService()
    bill_details = billing_service.get_bill_details(session, bill_id)

    if not bill_details:
        raise HTTPException(404, "Bill not found")

    return bill_details


@router.get("/{trip_id}/settlement")
def get_trip_settlement(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = driver and trip.driver_id == driver.id

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    settlement = session.exec(
        select(TripSettlement).where(TripSettlement.trip_id == trip_id)
    ).first()

    if not settlement:
        raise HTTPException(404, "Settlement not generated yet")

    billing_service = BillingService()
    settlement_details = billing_service.get_settlement_details(session, settlement.id)

    return settlement_details


@router.get("/{trip_id}/summary")
def get_trip_summary(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = driver and trip.driver_id == driver.id

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    trip_service = TripService()
    summary = trip_service.get_trip_summary(session, trip_id, is_driver)

    if not summary:
        raise HTTPException(500, "Could not generate trip summary")

    return summary


@router.get("/{trip_id}/bills")
def list_trip_bills(
    trip_id: int,
    only_unpaid: bool = False,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False
    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = bool(driver and trip.driver_id == driver.id)
    if not (is_user or is_driver):
        raise HTTPException(403, "Not authorized")

    stmt = select(TripBill).where(
        TripBill.trip_id == trip_id,
        TripBill.bill_type == "daily_bill",
    )
    if only_unpaid:
        stmt = stmt.where(not TripBill.is_paid)
    return session.exec(stmt.order_by(TripBill.bill_date)).all()


@router.post("/bill/{bill_id}/pay")
def user_pay_bill(
    bill_id: int,
    pay_req: BillPaymentRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    bill = session.get(TripBill, bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    if bill.user_id != current_user.id:
        raise HTTPException(403, "Not authorized for this bill")

    trip = session.get(Trip, bill.trip_id)
    if trip and trip.payment_method != "trip_day":
        raise HTTPException(
            400,
            f"Daily bills cannot be paid individually for the '{trip.payment_method}' payment method. Please pay the final settlement at the end of the trip.",
        )

    payment_service = PaymentService(redis_client)
    ok, err = payment_service.pay_bill_online(
        session, bill_id, current_user.id, pay_req.payment_method
    )
    if not ok:
        raise HTTPException(400, err or "Payment failed")

    try:
        send_push_notification(
            session=session,
            user_ids=[bill.user_id],
            title="Bill paid",
            body=f"Bill #{bill_id} of ₹{bill.total_amount:.2f} settled. Thank you!",
            data={"type": "bill_paid", "bill_id": bill_id, "trip_id": bill.trip_id},
        )
        driver_user = session.exec(
            select(User)
            .join(Driver, Driver.user_id == User.id)
            .where(Driver.id == bill.driver_id)
        ).first()
        if driver_user:
            send_push_notification(
                session=session,
                user_ids=[driver_user.id],
                title="Payment received",
                body=f"Bill #{bill_id} of ₹{bill.total_amount:.2f} paid by user.",
                data={"type": "bill_paid", "bill_id": bill_id, "trip_id": bill.trip_id},
            )
    except Exception:
        pass

    return {
        "message": "Bill paid successfully",
        "bill_id": bill_id,
        "trip_id": bill.trip_id,
        "amount": bill.total_amount,
    }


@router.post("/bill/{bill_id}/mark-paid-by-driver")
def driver_mark_bill_paid(
    bill_id: int,
    mark_req: MarkBillPaidRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only the assigned driver can mark a bill paid")

    bill = session.get(TripBill, bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    if bill.driver_id != driver.id:
        raise HTTPException(403, "Not authorized for this bill")

    trip = session.get(Trip, bill.trip_id)
    if trip and trip.payment_method != "trip_day":
        raise HTTPException(
            400,
            f"Daily bills cannot be paid individually for the '{trip.payment_method}' payment method.",
        )

    payment_service = PaymentService(redis_client)
    ok, err = payment_service.mark_bill_paid_offline(
        session, bill_id, driver.id, mark_req.note
    )
    if not ok:
        raise HTTPException(400, err or "Mark-paid failed")

    try:
        send_push_notification(
            session=session,
            user_ids=[bill.user_id],
            title="Bill marked paid",
            body=f"Driver confirmed cash payment for bill #{bill_id} (₹{bill.total_amount:.2f}).",
            data={
                "type": "bill_paid_offline",
                "bill_id": bill_id,
                "trip_id": bill.trip_id,
            },
        )
        driver_user = session.exec(
            select(User).where(User.id == driver.user_id)
        ).first()
        if driver_user:
            send_push_notification(
                session=session,
                user_ids=[driver_user.id],
                title="Cash payment recorded",
                body=f"Bill #{bill_id} marked paid (₹{bill.total_amount:.2f}).",
                data={
                    "type": "bill_paid_offline",
                    "bill_id": bill_id,
                    "trip_id": bill.trip_id,
                },
            )
    except Exception:
        pass

    return {
        "message": "Bill marked as paid by driver",
        "bill_id": bill_id,
        "trip_id": bill.trip_id,
        "amount": bill.total_amount,
        "paid_by": "driver_offline",
    }


@router.post("/settlement/{settlement_id}/pay")
def pay_trip_settlement(
    settlement_id: int,
    pay_req: BillPaymentRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """User pays the final trip settlement for advance_20 or full_payment methods."""
    settlement = session.get(TripSettlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "Settlement not found")

    if settlement.user_id != current_user.id:
        raise HTTPException(403, "Not authorized for this settlement")

    if settlement.user_payment_status == "paid":
        raise HTTPException(400, "Settlement is already paid")

    if settlement.remaining_due <= 0:
        raise HTTPException(400, "No remaining amount due")

    payment_service = PaymentService(redis_client)
    success, txn_id = payment_service.process_dummy_payment(
        amount=settlement.remaining_due,
        payer_id=str(current_user.id),
        payer_type="user",
        payment_method=pay_req.payment_method,
    )

    if not success:
        raise HTTPException(400, f"Payment failed: {txn_id}")

    # Record payment transaction
    payment_txn = PaymentTransaction(
        trip_id=settlement.trip_id,
        user_id=settlement.user_id,
        payer_type="user",
        payment_type="settlement",
        amount=settlement.remaining_due,
        payment_status="success",
        payment_method=pay_req.payment_method,
        gateway_transaction_id=txn_id,
        completed_at=now_ist(),
    )
    session.add(payment_txn)

    settlement.user_payment_status = "paid"
    settlement.paid_at = now_ist()
    session.add(settlement)

    # Optional backend cleanup: mark all associated unpaid daily bills as paid
    unpaid_bills = session.exec(
        select(TripBill).where(
            TripBill.trip_id == settlement.trip_id, not TripBill.is_paid
        )
    ).all()
    for bill in unpaid_bills:
        bill.is_paid = True
        bill.amount_paid = bill.total_amount
        bill.amount_due = 0.0
        bill.paid_at = now_ist()
        bill.paid_by = "user_online"
        session.add(bill)

    session.commit()

    try:
        send_push_notification(
            session=session,
            user_ids=[settlement.user_id],
            title="Settlement Paid",
            body=f"Your final settlement of ₹{settlement.remaining_due:.2f} has been paid successfully.",
            data={"type": "settlement_paid", "trip_id": settlement.trip_id},
        )
    except Exception:
        pass

    return {
        "message": "Settlement paid successfully",
        "settlement_id": settlement.id,
        "trip_id": settlement.trip_id,
        "amount_paid": settlement.remaining_due,
    }
