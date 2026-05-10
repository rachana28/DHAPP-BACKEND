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
    UserPaymentRequest,
    BillPaymentRequest,
    MarkBillPaidRequest,
    FareEstimateRequest,
    TripAttendance,
    TripBill,
    TripSettlement,
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

TIER_SIZE = 3  # Configurable: How many drivers per batch


@router.post("/estimate-fare")
def estimate_fare_for_booking(
    fare_req: FareEstimateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Compute the bill BEFORE creating a trip. Same calculator runs on /book-request,
    so the user always sees the exact amount they will be charged.

    Returns:
      {
        "total": 3622.5,
        "subtotal": 3450.0,
        "tax": 172.5,
        "currency": "INR",
        "components": [{name, amount}, ...],
        "meta": {hiring_type, num_days, hours_per_day, distance_km, is_night_booking, ...}
      }
    """
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
    """
    Step 1: User creates booking. System Segregates & Offers to Tier 1.
    """
    if not trip_in.vehicle_type:
        raise HTTPException(400, "Vehicle type is required.")

    # Block parallel bookings while another trip is mid-flow
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

    # Block new bookings if any prior trip has an unpaid daily bill
    payment_service = PaymentService(None)
    if payment_service.user_has_unpaid_bills(session, current_user.id):
        raise HTTPException(
            409,
            "You have unpaid bills from previous trips. Please settle them before booking a new trip.",
        )

    # 1. Save Trip
    trip_data = trip_in.model_dump()

    # --- Calculate Dates for Monthly Bookings ---
    if trip_data.get("hiring_type") == "Monthly":
        if not trip_data.get("start_date") or not trip_data.get("end_date"):
            today = today_ist()
            # Start date is the 1st of the current month
            start_date = today.replace(day=1)
            months_count = trip_data.get("months") or 1

            # Calculate end date based on no. of months
            end_month = start_date.month + months_count - 1
            end_year = start_date.year + (end_month // 12)
            end_month = (end_month % 12) + 1

            # Get the last day of the target month
            _, last_day = calendar.monthrange(end_year, end_month)
            end_date = date(end_year, end_month, last_day)

            trip_data["start_date"] = start_date
            trip_data["end_date"] = end_date
    else:
        # Enforce date requirements for other hiring types
        if not trip_data.get("start_date") or not trip_data.get("end_date"):
            raise HTTPException(
                400, "start_date and end_date are required for this hiring type."
            )
    # --------------------------------------------------------

    # ── Server-side fare calculation (authoritative — ignore any client-supplied fare) ──
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
    # Backfill resolved distance onto the Trip row so reports/refund logic see it.
    if trip_data.get("distance_km") is None and fare_dict["meta"].get("distance_km"):
        trip_data["distance_km"] = fare_dict["meta"]["distance_km"]

    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"

    db_trip = Trip.model_validate(trip_data)

    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    # 2. Rank Drivers (Intelligent Algorithm)
    ranked_drivers = rank_drivers(session, trip_in.vehicle_type)

    if not ranked_drivers:
        db_trip.status = "no_drivers_found"
        session.add(db_trip)
        session.commit()
        return db_trip

    # 3. Offer to Tier 1
    tier_1_drivers = ranked_drivers[:TIER_SIZE]
    create_offers_for_tier(session, db_trip.id, tier_1_drivers, tier=1)

    return db_trip


@router.get("/my-bookings", response_model=List[Union[TripReadUser, TripSafe]])
def get_my_bookings(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Get bookings for the current user.
    - If User: Returns their trip history (Active & Past) with Driver details.
    - If Driver: Returns trips they are assigned to.
    """
    if current_user.role == "driver":
        # Existing Driver Logic
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        if not driver:
            return []
        return session.exec(select(Trip).where(Trip.driver_id == driver.id)).all()

    elif current_user.role == "user":
        # Driver details must NOT be exposed until the trip is finalized (both payments done).
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

        # Strip driver field for trips not yet finalized (mutates the loaded ORM object,
        # but only within this read session — never committed).
        for t in trips:
            if t.status in DRIVER_HIDDEN_STATES:
                t.driver = None
                t.driver_id = None  # also hide id so frontend can't fetch separately
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
    """
    User cancels a trip.

    Honors payment-method rules:
    - trip_day: cancel allowed any time before actual start; full refund
    - advance_20: not cancelable once any successful payment recorded
    - full_payment: not cancelable once any successful payment recorded
    Also refunds any user payment per PaymentService.calculate_refund_amount.
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    # Determine whether user or driver is calling
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

    # Block cancellation while any daily bill is outstanding
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

    # State transition (skip strict validation: allow from many states)
    new_state = "cancelled_by_user" if is_user else "cancelled_by_driver"
    trip.status = new_state
    session.add(trip)

    # Remove any open driver offers
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
    """
    Get all pending offers for the logged-in driver.
    Uses 'TripOfferPublic' to ensure NO sensitive credentials (user_id/driver_id) are exposed.
    """
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    # Eager load the trip details
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
    """
    DEPRECATED: legacy single-step accept. Use POST /trips/driver/{trip_id}/accept-and-pay
    followed by POST /trips/driver/{trip_id}/process-payment so the driver acceptance fee
    flow runs. Kept temporarily for client backward-compat.
    """
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

    # 1. Assign Trip
    trip.driver_id = driver.id
    trip.status = "accepted"

    # 2. Update Accepted Offer
    offer.status = "accepted"
    session.add(trip)
    session.add(offer)

    # 3. DELETE all other offers for this trip (Requirement: automatically deleted)
    other_offers = session.exec(
        select(TripOffer).where(TripOffer.trip_id == trip.id)
    ).all()
    for o in other_offers:
        if o.id != offer.id:
            session.delete(o)

    session.commit()

    # Invalidate cache if needed
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
    """
    Driver rejects an offer.
    """
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Not authorized")

    offer = session.get(TripOffer, offer_id)
    if not offer or offer.driver_id != driver.id:
        raise HTTPException(404, "Offer not found")

    # 1. Mark as Rejected
    offer.status = "rejected"
    session.add(offer)
    session.commit()  # Commit the rejection first

    # 2. Update: Instant Check
    # Check if this rejection triggers next tier or cancellation
    trip = session.get(Trip, offer.trip_id)
    if trip and trip.status == "searching":
        escalated = attempt_trip_escalation(session, trip)
        if escalated:
            session.commit()  # Commit the escalation/cancellation change

    return {"message": "Offer rejected"}


@router.post("/check-escalation")
def check_and_escalate_tiers(session: Session = Depends(get_session)):
    """
    Manual trigger endpoint (useful for testing/debugging).
    """
    count = process_tier_escalation(session)
    return {"message": f"Escalated {count} trips."}


# ============= NEW PAYMENT & TRIP MANAGEMENT APIS =============


@router.post("/{trip_id}/select-payment-method")
def select_payment_method(
    trip_id: int,
    payment_req: PaymentMethodSelect,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    User selects payment method after driver is assigned but before payment

    Payment Methods:
    - trip_day: Pay for each day
    - advance_20: Pay 20% upfront, 80% at end
    - full_payment: Pay full amount upfront with 5% discount
    """
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
    """
    Driver accepts trip offer and initiates payment
    - Driver sees trip offer
    - Driver clicks "Accept"
    - Driver must pay fixed amount to finalize
    - Sets timer for payment (30 min default)

    Returns:
    - If accept: Payment required amount and timer
    - If reject: Trip re-offered to next driver
    """
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
        # Driver rejects trip - mark offer as rejected and escalate.
        # Also: if driver had already paid the acceptance fee, refund it and reset trip.
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

        # Reset assignment so allocation can re-tier
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
        # Driver accepts trip
        trip.driver_id = driver.id
        trip.status = "accepted_pending_payment"
        trip.driver_accepted_at = (
            now_ist()
        )  # Use dedicated field; preserve booking_time

        # Store timer in Redis (30 min to pay)
        redis_key = f"driver_payment_timer:{trip_id}:{driver.id}"
        redis_client.setex(redis_key, 1800, "pending")  # 30 min

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
    """
    Driver processes payment to finalize trip acceptance

    After successful payment:
    - Driver payment is locked
    - Trip transitions to "payment_in_progress"
    - Wait for user payment
    """
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

    # Process payment
    payment_service = PaymentService(redis_client)
    success, error = payment_service.driver_accept_payment(session, trip_id, driver.id)

    if not success:
        raise HTTPException(400, f"Payment failed: {error}")

    # Update trip status
    trip_service = TripService()
    success, error = trip_service.transition_trip_state(
        session, trip_id, "payment_in_progress", validate=True
    )

    if not success:
        raise HTTPException(400, f"Status update failed: {error}")

    # Clear timer
    redis_key = f"driver_payment_timer:{trip_id}:{driver.id}"
    redis_client.delete(redis_key)

    return {
        "message": "Driver payment successful. Waiting for user payment.",
        "trip_id": trip_id,
        "trip_status": trip.status,
    }


@router.post("/{trip_id}/user-payment")
def user_process_payment(
    trip_id: int,
    payment_req: UserPaymentRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    User makes payment for trip based on selected payment method

    Payment Method Effects:
    - trip_day: Pays for 1 day only
    - advance_20: Pays 20% of total fare
    - full_payment: Pays full amount (5% discount applied)

    After payment: Trip moves to active_pending_otp
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Not authorized")

    if trip.status != "payment_in_progress":
        raise HTTPException(400, f"Trip status is {trip.status}, cannot pay now")

    if not trip.payment_method:
        raise HTTPException(400, "Payment method not selected")

    # Validate payment amount based on method
    if not trip.fare:
        raise HTTPException(400, "Trip fare not set")

    if trip.payment_method == "trip_day":
        # For trip_day, amount should be calculated per day (1 day's fare)
        expected_amount = trip.fare
    elif trip.payment_method == "advance_20":
        expected_amount = trip.fare * 0.20
    elif trip.payment_method == "full_payment":
        # Full payment has 5% discount
        expected_amount = trip.fare * 0.95
    else:
        raise HTTPException(400, "Invalid payment method on trip")

    if abs(payment_req.amount - expected_amount) > 0.01:
        raise HTTPException(
            400,
            f"Payment amount mismatch. Expected {expected_amount:.2f}, got {payment_req.amount:.2f}",
        )

    # Process payment
    payment_service = PaymentService(redis_client)
    success, error = payment_service.user_make_payment(
        session,
        trip_id,
        str(current_user.id),
        payment_req.amount,
        payment_req.payment_method,
    )

    if not success:
        raise HTTPException(400, f"Payment failed: {error}")

    # After successful payment, generate scheduled times
    trip_service = TripService()

    if trip.start_date:
        duration_hours = trip_service.get_trip_duration_hours(trip.shift_details)
        trip_start_time = trip_service.get_trip_start_time(
            trip.shift_details, trip.start_date
        )

        if trip_start_time:
            trip.scheduled_start_time = trip_start_time
            trip.scheduled_end_time = (
                trip_start_time + timedelta(hours=duration_hours)
                if duration_hours
                else None
            )
            trip.trip_duration_hours = duration_hours

        # Create attendance records (single-day if no end_date), honoring selected_days
        end_date_for_attendance = trip.end_date or trip.start_date
        effective_start_dt = trip_start_time or datetime.combine(
            trip.start_date, datetime.min.time()
        )
        att_ok, att_err = trip_service.create_trip_attendance_records(
            session,
            trip_id,
            trip.start_date,
            end_date_for_attendance,
            effective_start_dt,
            duration_hours or 8,
            selected_days=trip.selected_days,
        )
        if not att_ok:
            raise HTTPException(400, att_err)

    # Transition to active_pending_otp (waiting for OTP)
    success, error = trip_service.transition_trip_state(
        session, trip_id, "active_pending_otp", validate=True
    )

    if not success:
        raise HTTPException(400, f"Status update failed: {error}")

    return {
        "message": "User payment successful. OTP will be sent before trip start.",
        "trip_id": trip_id,
        "scheduled_start": trip.scheduled_start_time.isoformat()
        if trip.scheduled_start_time
        else None,
    }


@router.post("/{trip_id}/request-otp")
def request_otp_for_trip(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    USER-only. Returns the trip-day OTP that the user should read out to the driver.
    Auto-generated by the scheduler 15 min before trip_start; this endpoint also
    works as resend (idempotent within validity window).

    Driver app MUST NOT call this endpoint — it 403s.
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can fetch the OTP")

    if trip.status not in ("active_pending_otp", "ongoing"):
        raise HTTPException(
            400, f"Trip status is {trip.status}, OTP cannot be generated"
        )

    if not trip.scheduled_start_time:
        raise HTTPException(400, "Trip schedule not set")

    if trip.is_payment_blocked:
        raise HTTPException(
            400,
            "Trip is paused — settle outstanding daily bills before requesting today's OTP.",
        )

    # Build today's start datetime (carry over time-of-day from scheduled_start_time)
    trip_day = today_ist()
    trip_start_for_day = trip.scheduled_start_time.replace(
        year=trip_day.year, month=trip_day.month, day=trip_day.day
    )

    otp_service = OTPService(redis_client)
    otp, error = otp_service.generate_otp(
        session, trip_id, trip_start_for_day, trip_date=trip_day
    )
    if error:
        raise HTTPException(400, error)

    # Push OTP to user (so the driver-side gets it via the user, not by polling).
    # P3 fix: Don't expose OTP in notification body/data (lock-screen privacy)
    try:
        send_push_notification(
            session=session,
            user_ids=[trip.user_id],
            title="Trip OTP Ready",
            body=f"Your trip OTP is ready. Open the app to view it and share with your driver to start trip #{trip_id}.",
            data={"type": "trip_otp", "trip_id": trip_id},  # Removed "otp" field
        )
    except Exception:
        pass

    expiry_time = otp_service.get_otp_expiry_time(session, trip_id, trip_day)
    validity_start = trip_start_for_day - timedelta(minutes=15)

    return {
        "message": "OTP generated. Share this with your driver to start the trip.",
        "otp": otp,  # delivered to user only — driver never sees this endpoint's response
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
    """
    DRIVER-only. The driver enters the OTP that the user shared verbally.
    On success, today's shift moves the trip to "ongoing" and stamps actual_start_time.
    """
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

    trip_day = today_ist()

    otp_service = OTPService(redis_client)
    is_valid, error = otp_service.verify_otp(
        session, trip_id, driver.id, otp_req.otp, trip_date=trip_day
    )
    if not is_valid:
        raise HTTPException(400, error)

    # Transition trip to "active" then immediately to "ongoing"
    trip_service = TripService()
    ok, err = trip_service.transition_trip_state(
        session, trip_id, "active", validate=True
    )
    if not ok:
        raise HTTPException(400, f"Status update failed: {err}")
    ok, err = trip_service.transition_trip_state(
        session, trip_id, "ongoing", validate=True
    )
    if not ok:
        raise HTTPException(400, f"Status update failed: {err}")

    trip.actual_start_time = now_ist()

    # Mark today's attendance OTP-verified (final 'present' is set on trip end)
    attendance = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.trip_date == trip_day,
        )
    ).first()
    if attendance:
        attendance.user_otp_verified = True
        attendance.driver_otp_verified = True
        attendance.actual_start = trip.actual_start_time
        session.add(attendance)
    session.add(trip)
    session.commit()

    # Push notification to user: trip started
    try:
        send_push_notification(
            session=session,
            user_ids=[trip.user_id],
            title="Trip started",
            body=f"Your driver verified the OTP. Trip #{trip_id} is now in progress.",
            data={"type": "trip_started", "trip_id": trip_id},
        )
    except Exception:
        pass  # Notifications must never block the API

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
    """
    User or Driver marks a day as skip/absent

    Skip Reasons:
    - user_emergency
    - driver_emergency
    - weather
    - vehicle_issue

    Result:
    - Day marked as absent
    - No charges for that day
    - If enough days skipped, may impact settlement
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    # Determine if user or driver
    is_user = trip.user_id == current_user.id
    marked_by = "user"

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        if not driver or trip.driver_id != driver.id:
            raise HTTPException(403, "Not authorized")
        marked_by = "driver"

    # Mark as absent
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
    """
    Driver manually ends trip after completed hours

    After ending:
    - Trip marked as "completed"
    - Billing starts
    - Settlement can be generated
    """
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

    # Determine if more shift days remain (multi-day booking).
    today = today_ist()
    has_future_shifts = (trip.end_date is not None) and (today < trip.end_date)

    trip_service = TripService()
    next_state = "active_pending_otp" if has_future_shifts else "completed"
    success, error = trip_service.transition_trip_state(
        session, trip_id, next_state, validate=True
    )
    if not success:
        raise HTTPException(400, error)

    # Set actual end time only when this was the final shift day
    # P0 fix: When transitioning to active_pending_otp, also advance scheduled times to next day
    if not has_future_shifts:
        # Normalize any inbound datetime to IST naive (accepts both naive and tz-aware).
        trip.actual_end_time = to_ist_naive(end_req.actual_end_time) or now_ist()
    else:
        # Multi-day trip: find next attendance day and update scheduled times
        next_attendance = session.exec(
            select(TripAttendance)
            .where(
                TripAttendance.trip_id == trip_id,
                TripAttendance.trip_date > today,
            )
            .order_by(TripAttendance.trip_date)
        ).first()

        if next_attendance:
            trip.scheduled_start_time = next_attendance.scheduled_start
            trip.scheduled_end_time = next_attendance.scheduled_end

    session.add(trip)
    session.commit()

    # Mark today's attendance as PRESENT (success after trip end, per requirement)
    trip_service.mark_trip_day_present(session, trip_id, today)

    # Generate daily bill
    billing_service = BillingService()
    bill_success, bill_id, bill_error = billing_service.generate_daily_bill(
        session, trip_id, today_ist()
    )

    # Push notification: bill ready, payment due
    if bill_success and bill_id:
        bill = session.get(TripBill, bill_id)
        amount = bill.total_amount if bill else 0.0
        try:
            send_push_notification(
                session=session,
                user_ids=[trip.user_id],
                title="Trip ended — bill ready",
                body=f"Trip #{trip_id} ended. Amount due: ₹{amount:.2f}. Pay to book your next trip.",
                data={
                    "type": "bill_generated",
                    "trip_id": trip_id,
                    "bill_id": bill_id,
                    "amount": amount,
                },
            )
            # Notify driver too so they can collect cash if needed
            driver_user = session.exec(
                select(User)
                .join(Driver, Driver.user_id == User.id)
                .where(Driver.id == driver.id)
            ).first()
            if driver_user:
                send_push_notification(
                    session=session,
                    user_ids=[driver_user.id],
                    title="Trip ended — bill generated",
                    body=f"Trip #{trip_id} bill ₹{amount:.2f} sent to user.",
                    data={
                        "type": "bill_generated",
                        "trip_id": trip_id,
                        "bill_id": bill_id,
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
        "trip_status": trip.status,  # active_pending_otp (more days) or completed (final)
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
    """
    Get detailed bill for a specific day

    Returns itemized breakdown with:
    - Base fare
    - Allowances
    - Taxes
    - Discounts
    - Total amount
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    # Check authorization
    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = driver and trip.driver_id == driver.id

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    # Validate the bill actually belongs to this trip (prevents cross-trip read)
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
    """
    Get final settlement after all trips completed

    Shows:
    - Total days worked
    - Total earned
    - Total paid upfront
    - Balance due/refund
    - Payment status
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    # Check authorization
    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = driver and trip.driver_id == driver.id

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    # Get settlement
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
    """
    Get comprehensive trip summary

    Shows:
    - Trip status
    - Days present/absent
    - Payment status
    - Billing information
    - Trip timeline
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    # Check authorization
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


# ============= DAILY-BILL SETTLEMENT APIS =============


@router.get("/{trip_id}/bills")
def list_trip_bills(
    trip_id: int,
    only_unpaid: bool = False,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """List all daily bills for a trip (user or assigned driver)."""
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
        stmt = stmt.where(TripBill.is_paid == False)  # noqa: E712
    return session.exec(stmt.order_by(TripBill.bill_date)).all()


@router.post("/bill/{bill_id}/pay")
def user_pay_bill(
    bill_id: int,
    pay_req: BillPaymentRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """USER pays a daily bill via the (dummy) payment gateway."""
    bill = session.get(TripBill, bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    if bill.user_id != current_user.id:
        raise HTTPException(403, "Not authorized for this bill")

    payment_service = PaymentService(redis_client)
    ok, err = payment_service.pay_bill_online(
        session, bill_id, current_user.id, pay_req.payment_method
    )
    if not ok:
        raise HTTPException(400, err or "Payment failed")

    # Notify both parties
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
    """
    DRIVER marks a daily bill as paid (user paid in cash directly).
    Equivalent to user_pay_bill in effect — unpauses the trip and unblocks future bookings.
    """
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

    payment_service = PaymentService(redis_client)
    ok, err = payment_service.mark_bill_paid_offline(
        session, bill_id, driver.id, mark_req.note
    )
    if not ok:
        raise HTTPException(400, err or "Mark-paid failed")

    # Notify both parties
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
