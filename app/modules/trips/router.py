"""Driver-hiring trip API (Daily / Monthly / Outstation).

User-app endpoints: fare estimate + booking (day-count limits enforced:
Monthly 4-21 scheduled days per calendar month, Daily <=21/month, Outstation
1-10 days with a mandatory '(HH:MM)' start time in shift_details), payment
method selection (trip_day / advance_20 / full_payment), upfront payment,
OTP request, skip-day, end-trip, cancel with refund/shortfall maths,
modify-schedule, bills (daily / advance_recovery / cancellation_balance) and
final-settlement payment.

Driver-app endpoints: offers, accept-and-pay (acceptance fee), process-payment,
withdraw (pre-shift), abandon (post-shift), verify-otp, skip-day (3 per 30-day
window), active-bookings, and offline cash collection (mark-paid-by-driver).

GET /{trip_id}/summary is role-aware: the user view carries payment internals
and the public driver block; the driver view carries only the rider's
name/avatar plus an `actions` block (can_verify_otp, can_skip, bills to
collect, ...) for the current-trip screen. Responses use reference ids only.
"""

import redis
from datetime import date, timedelta
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    BackgroundTasks,
    Header,
    Query,
)
from sqlmodel import Session, select, desc
from typing import List, Optional
from sqlalchemy.orm import selectinload

from app.core import cache
from app.core.database import get_session, get_redis
from app.core.idempotency import IdempotencyGuard, idempotent
from app.core.models import (
    Trip,
    TripCreate,
    TripOffer,
    TripOfferPublic,
    TripReadUser,
    TripReadDriver,
    TripSafe,
    TripBillRead,
    Driver,
    User,
    FareEstimateRequest,
    TripAttendance,
    TripBill,
    TripSettlement,
    TripDaySkipRequest,
    Payment,
)
from app.modules.payments import service as central_payments
from app.core.security import get_current_user
from app.modules.trips.allocation import (
    rank_drivers,
    create_offers_for_tier,
    process_tier_escalation,
    attempt_trip_escalation,
)
from app.modules.trips.otp_service import OTPService
from app.modules.trips.payment_service import (
    PaymentService,
    get_driver_acceptance_fee,
    get_driver_abandon_suspension_hours,
)
from app.modules.trips.trip_service import TripService
from app.modules.trips.billing_service import BillingService
from app.modules.trips.pricing_calculator import (
    calculate_fare,
    validate_day_counts,
    validate_pricing_inputs,
)
from app.utils.time_utils import now_ist, today_ist, to_ist_naive
from app.services.dues import raise_if_unpaid_past_due
from app.utils.notifications import send_push_notification
from app.utils.id_generator import (
    generate_reference_id,
    get_by_reference,
    trip_subtype,
    TRIP,
    PAYMENT,
)

router = APIRouter(prefix="/trips", tags=["Trips"])


def _trip_read_driver(t: Trip, skips_remaining: Optional[int]) -> TripReadDriver:
    return TripReadDriver(
        reference_id=t.reference_id,
        **{
            k: getattr(t, k)
            for k in TripReadDriver.model_fields.keys()
            if k not in ("user", "driver_skips_remaining", "id")
        },
        user={
            "full_name": t.user.full_name if t.user else None,
            "avatar_url": getattr(t.user, "avatar_url", None) if t.user else None,
        }
        if t.user
        else None,
        driver_skips_remaining=skips_remaining,
    )


TIER_SIZE = 3

_ONLINE_PAY_CHANNELS = ("platform", "wallet")


def _validate_online_channel(channel: str) -> None:
    if channel not in _ONLINE_PAY_CHANNELS:
        raise HTTPException(400, f"channel must be one of {list(_ONLINE_PAY_CHANNELS)}")


_DRIVER_FEE_CHANNELS = {"platform", "upi", "provider_wallet"}


def _validate_driver_channel(channel: str) -> None:
    if channel not in _DRIVER_FEE_CHANNELS:
        raise HTTPException(
            400,
            "Acceptance fee channel must be one of "
            f"{sorted(_DRIVER_FEE_CHANNELS)} (card / UPI / provider wallet).",
        )


def _maybe_schedule_platform_settlement(background_tasks, payment) -> None:
    if (
        payment is not None
        and payment.channel in ("platform", "upi")
        and payment.status == "pending"
    ):
        background_tasks.add_task(
            central_payments.simulate_webhook_delivery, payment.reference_id
        )


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
        shift_details=fare_req.shift_details,
    )
    if not ok:
        raise HTTPException(400, err)

    ok, err = validate_day_counts(
        hiring_type=fare_req.hiring_type,
        start_date=fare_req.start_date,
        end_date=fare_req.end_date,
        selected_days=fare_req.selected_days,
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

    if (trip_in.hiring_type or "").strip() in ("Tow Service", "Mechanic Service"):
        target = (
            "/tow-trips/book-request"
            if trip_in.hiring_type.strip() == "Tow Service"
            else "/mechanic-trips/book-request"
        )
        raise HTTPException(
            400,
            f"hiring_type='{trip_in.hiring_type}' is not accepted here. "
            f"Use {target} for this booking kind.",
        )

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
            f"You already have an active trip (id={in_flight.reference_id}, "
            f"status={in_flight.status}). Complete or cancel it before booking another.",
        )

    payment_service = PaymentService(None)
    has_dues, dues_kind = payment_service.user_has_outstanding_dues(
        session, current_user.id
    )
    if has_dues:
        msg = (
            "You have an unpaid daily bill from a previous trip. Please pay it before booking a new trip."
            if dues_kind == "bill"
            else "You have an unpaid final settlement from a previous trip. Please clear it before booking a new trip."
        )
        raise HTTPException(409, msg)

    raise_if_unpaid_past_due(session, current_user.id)

    trip_data = trip_in.model_dump()

    trip_data["driver_id"] = None
    trip_data.pop("tow_truck_driver_id", None)

    if not trip_data.get("start_date") or not trip_data.get("end_date"):
        raise HTTPException(400, "start_date and end_date are required.")

    if trip_data.get("hiring_type") == "Monthly":
        day_filter = TripService().parse_selected_days(trip_data.get("selected_days"))
        if not day_filter:
            raise HTTPException(
                400,
                "selected_days is required for Monthly bookings (e.g. 'Mon,Tue,Fri').",
            )

        s = trip_data["start_date"]
        snapped_start = None
        for _ in range(7):
            if s.weekday() in day_filter:
                snapped_start = s
                break
            s = s + timedelta(days=1)

        e = trip_data["end_date"]
        snapped_end = None
        for _ in range(7):
            if e.weekday() in day_filter:
                snapped_end = e
                break
            e = e - timedelta(days=1)

        if snapped_start is None or snapped_end is None or snapped_start > snapped_end:
            raise HTTPException(
                400,
                "No selected weekday falls within the chosen "
                "start_date / end_date range.",
            )

        trip_data["start_date"] = snapped_start
        trip_data["end_date"] = snapped_end

    today = today_ist()
    if trip_data.get("start_date") and trip_data["start_date"] < today:
        raise HTTPException(400, "start_date cannot be in the past.")
    if (
        trip_data.get("end_date")
        and trip_data.get("start_date")
        and trip_data["end_date"] < trip_data["start_date"]
    ):
        raise HTTPException(400, "end_date cannot be before start_date.")

    ok, err = validate_pricing_inputs(
        hiring_type=trip_data.get("hiring_type"),
        distance_km=trip_data.get("distance_km"),
        start_lat=trip_data.get("start_lat"),
        start_lng=trip_data.get("start_lng"),
        end_lat=trip_data.get("end_lat"),
        end_lng=trip_data.get("end_lng"),
        end_location=trip_data.get("end_location"),
        shift_details=trip_data.get("shift_details"),
    )
    if not ok:
        raise HTTPException(400, err)

    ok, err = validate_day_counts(
        hiring_type=trip_data.get("hiring_type"),
        start_date=trip_data.get("start_date"),
        end_date=trip_data.get("end_date"),
        selected_days=trip_data.get("selected_days"),
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

    trip_data["payment_method"] = "trip_day"

    db_trip = Trip.model_validate(trip_data)
    db_trip.reference_id = generate_reference_id(
        session, TRIP, subtype=trip_subtype(db_trip.hiring_type)
    )

    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    ranked_drivers = rank_drivers(session, trip_in.vehicle_type)

    if not ranked_drivers:
        return db_trip

    tier_1_drivers = ranked_drivers[:TIER_SIZE]
    create_offers_for_tier(session, db_trip.id, tier_1_drivers, tier=1)

    return db_trip


@router.get("/my-bookings")
def get_my_bookings(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    if current_user.role == "driver":
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        if not driver:
            return []
        trips = session.exec(
            select(Trip)
            .where(Trip.driver_id == driver.id)
            .order_by(desc(Trip.booking_time))
            .offset(offset)
            .limit(limit)
            .options(selectinload(Trip.user))
        ).all()
        trip_service = TripService()
        return [
            _trip_read_driver(t, trip_service.driver_skips_remaining(session, t.id))
            for t in trips
        ]

    elif current_user.role == "user":
        DRIVER_HIDDEN_STATES = {
            "searching",
            "no_drivers_found",
            "accepted_pending_payment",
            "payment_in_progress",
            "payment_failed",
            "rejected",
        }

        ride_rows = session.exec(
            select(Trip)
            .where(Trip.user_id == current_user.id)
            .order_by(desc(Trip.booking_time))
            .offset(offset)
            .limit(limit)
            .options(selectinload(Trip.driver))
        ).all()

        result: list = []

        for t in ride_rows:
            view = TripReadUser.model_validate(t, from_attributes=True)
            if t.status in DRIVER_HIDDEN_STATES:
                view.driver = None
            result.append(view)

        return result
    else:
        return []


@router.get("/driver/active-bookings")
def get_driver_active_bookings(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Driver-app polling target: the driver's currently-engaged trips only."""
    if current_user.role != "driver":
        return []
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        return []

    key = cache.active_key("driver_trips", driver.id)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached

    trips = session.exec(
        select(Trip)
        .where(
            Trip.driver_id == driver.id,
            Trip.status.in_(TripService.DRIVER_BUSY_STATES),
        )
        .order_by(desc(Trip.booking_time))
        .options(selectinload(Trip.user))
    ).all()
    trip_service = TripService()
    result = [
        _trip_read_driver(
            t, trip_service.driver_skips_remaining(session, t.id)
        ).model_dump(mode="json")
        for t in trips
    ]
    cache.cache_set_json(key, result, cache.ACTIVE_CACHE_TTL)
    return result


@router.post("/{trip_id}/cancel")
def cancel_trip(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """User-initiated cancellation with refund/shortfall maths (F6)."""
    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can cancel this booking")

    trip_service = TripService()
    can_cancel, reason, has_completed_shift = trip_service.can_cancel_trip(
        session, trip_id, user_id=str(current_user.id)
    )
    if not can_cancel:
        raise HTTPException(400, reason or "Trip cannot be cancelled")

    payment_service = PaymentService(redis_client)

    if trip.payment_method == "trip_day" and payment_service.trip_has_unpaid_bills(
        session, trip_id
    ):
        raise HTTPException(
            400,
            "Pay the outstanding daily bill before cancelling the remaining shifts.",
        )

    user_refund = 0.0
    cancellation_deduction = 0.0
    shortfall_amount = 0.0
    cancellation_bill_id = None
    if trip.payment_method in ("advance_20", "full_payment"):
        settle = payment_service.calculate_user_cancel_settlement(session, trip_id)
        cancellation_deduction = settle["anti_fraud"]
        user_refund = settle["refund_amount"]
        shortfall_amount = settle["shortfall_amount"]

        if user_refund > 0:
            ok, err = payment_service.process_refund(
                session,
                trip_id,
                user_refund,
                reason=(
                    "Trip cancelled by user"
                    if cancellation_deduction <= 0
                    else f"Trip cancelled by user "
                    f"(₹{cancellation_deduction:.2f} skip deduction)"
                ),
            )
            if not ok:
                raise HTTPException(400, err or "Refund failed")
        elif shortfall_amount > 0:
            from datetime import date as _date

            bill = TripBill(
                trip_id=trip_id,
                user_id=trip.user_id,
                driver_id=trip.driver_id or 0,
                bill_type="cancellation_balance",
                bill_date=_date.today(),
                total_amount=shortfall_amount,
                amount_paid=0.0,
                amount_due=shortfall_amount,
                is_generated=True,
                is_paid=False,
                components=[
                    {
                        "name": "Cancellation balance",
                        "amount": shortfall_amount,
                        "served_charge": settle["served_charge"],
                        "anti_fraud": settle["anti_fraud"],
                        "upfront_paid": settle["upfront_paid"],
                    }
                ],
                notes="Outstanding amount after mid-trip cancellation maths.",
            )
            session.add(bill)
            session.flush()
            cancellation_bill_id = bill.id

    driver_refund = 0.0
    if trip.driver_id and not has_completed_shift:
        driver_refund = payment_service.calculate_driver_fee_refund(
            session, trip_id, trip.driver_id
        )
        if driver_refund > 0:
            ok, err = payment_service.refund_driver_acceptance_fee(
                session,
                trip_id,
                trip.driver_id,
                reason="Trip cancelled by user",
            )
            if not ok:
                raise HTTPException(400, err or "Driver fee refund failed")
            trip.driver_payment_status = "unpaid"
            trip.driver_payment_amount = None

    pending_atts = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
    ).all()
    for att in pending_atts:
        att.status = "skipped_by_user"
        att.skip_reason = "Trip cancelled by user"
        att.marked_by = "user"
        session.add(att)

    if shortfall_amount > 0:
        trip.status = "cancelled_by_user"
        if trip.actual_end_time is None:
            trip.actual_end_time = now_ist()
    elif has_completed_shift:
        trip.status = "completed"
        if trip.actual_end_time is None:
            trip.actual_end_time = now_ist()
    else:
        trip.status = "cancelled_by_user"
    trip.state_version += 1
    session.add(trip)

    cancelled_driver_id = trip.driver_id

    for offer in session.exec(
        select(TripOffer).where(TripOffer.trip_id == trip.id)
    ).all():
        if offer.status == "pending":
            session.delete(offer)

    session.commit()

    settlement_id = None
    if shortfall_amount > 0:
        trip_service.transition_trip_state(
            session, trip_id, "cancellation_pending_payment", validate=False
        )
    elif has_completed_shift:
        billing_service = BillingService()
        ok, sid, _ = billing_service.generate_final_settlement(session, trip_id)
        if ok or sid:
            settlement_id = sid

    trip.driver_id = None
    trip.driver_accepted_at = None
    session.add(trip)
    session.commit()

    if cancelled_driver_id and redis_client:
        try:
            redis_client.delete(f"driver_{cancelled_driver_id}")
        except Exception:
            pass

    if cancelled_driver_id:
        try:
            driver_user = session.exec(
                select(User)
                .join(Driver, Driver.user_id == User.id)
                .where(Driver.id == cancelled_driver_id)
            ).first()
            if driver_user:
                send_push_notification(
                    session=session,
                    user_ids=[driver_user.id],
                    title="Trip cancelled",
                    body=f"Trip #{trip_id} was cancelled by the user.",
                    data={"type": "trip_cancelled", "trip_id": trip.reference_id},
                )
        except Exception:
            pass

    return {
        "message": "Trip cancelled successfully",
        "refund_amount": user_refund,
        "cancellation_deduction": cancellation_deduction,
        "shortfall_amount": shortfall_amount,
        "cancellation_bill_id": cancellation_bill_id,
        "trip_status": trip.status,
        "settlement_id": settlement_id,
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
        .join(Trip, TripOffer.trip_id == Trip.id)
        .where(TripOffer.driver_id == driver.id)
        .where(TripOffer.status == "pending")
        .where(Trip.status == "searching")
        .order_by(desc(TripOffer.created_at))
        .limit(20)
        .options(selectinload(TripOffer.trip))
    )

    offers = session.exec(statement).all()
    return offers


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

    offer = session.exec(
        select(TripOffer).where(TripOffer.id == offer_id).with_for_update()
    ).first()
    if not offer or offer.driver_id != driver.id:
        raise HTTPException(404, "Offer not found")

    if offer.status != "pending":
        raise HTTPException(400, f"Offer is already {offer.status}")

    offer.status = "rejected"
    session.add(offer)
    session.commit()

    trip = session.exec(
        select(Trip).where(Trip.id == offer.trip_id).with_for_update()
    ).first()
    if trip and trip.status == "searching":
        escalated = attempt_trip_escalation(session, trip)
        if escalated:
            session.commit()

    return {"message": "Offer rejected"}


@router.post("/{trip_id}/modify-schedule")
def modify_trip_schedule(
    trip_id: str,
    selected_days: Optional[str] = Body(None, embed=True),
    end_date: Optional[date] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Monthly-only: change ``selected_days`` and/or extend ``end_date`` (F12)."""
    from datetime import date as _date

    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can modify this booking")
    if (trip.hiring_type or "").strip().lower() != "monthly":
        raise HTTPException(
            400, "Schedule modification is only supported for Monthly trips"
        )
    if trip.status not in ("active_pending_otp", "paused"):
        raise HTTPException(
            400,
            f"Schedule cannot be modified in status '{trip.status}'. "
            "Wait for the current shift to end first.",
        )

    next_att = session.exec(
        select(TripAttendance)
        .where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status == "scheduled",
        )
        .order_by(TripAttendance.scheduled_start)
        .limit(1)
    ).first()
    if next_att and next_att.scheduled_start - now_ist() < timedelta(hours=24):
        raise HTTPException(
            400,
            "Schedule modification requires at least 24h before the next scheduled shift.",
        )

    new_selected_days = selected_days or trip.selected_days
    new_end_date = end_date or trip.end_date
    if not new_end_date:
        raise HTTPException(400, "end_date is required to modify schedule")
    if new_end_date < (trip.start_date or _date.today()):
        raise HTTPException(400, "end_date cannot precede trip.start_date")

    window_start = max(trip.start_date or today_ist(), today_ist())
    ok, err = validate_day_counts(
        hiring_type=trip.hiring_type,
        start_date=window_start,
        end_date=new_end_date,
        selected_days=new_selected_days,
    )
    if not ok:
        raise HTTPException(400, err)

    fare_quote = calculate_fare(
        session,
        redis_client,
        hiring_type=trip.hiring_type,
        vehicle_type=trip.vehicle_type,
        shift_details=trip.shift_details,
        start_date=trip.start_date,
        end_date=new_end_date,
        months=None,
        selected_days=new_selected_days,
        start_location=trip.start_location,
        end_location=trip.end_location,
        start_lat=trip.start_lat,
        start_lng=trip.start_lng,
        end_lat=trip.end_lat,
        end_lng=trip.end_lng,
        distance_km=trip.distance_km,
        booking_time=trip.booking_time,
    )
    new_fare = fare_quote["total"]
    old_fare = float(trip.fare or 0.0)
    fare_diff = round(new_fare - old_fare, 2)

    pending_future = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status == "scheduled",
        )
    ).all()
    if pending_future:
        from app.core.models import OTPRegistry

        att_ids = [a.id for a in pending_future]
        stale_otps = session.exec(
            select(OTPRegistry).where(OTPRegistry.attendance_id.in_(att_ids))
        ).all()
        for o in stale_otps:
            session.delete(o)
    for a in pending_future:
        session.delete(a)
    session.flush()

    schedule_anchor_start_dt = trip.scheduled_start_time or now_ist()
    trip_service_inst = TripService()
    if next_att:
        anchor_dt = next_att.scheduled_start
    else:
        anchor_dt = schedule_anchor_start_dt
    duration_hours = trip.trip_duration_hours or 8

    start_for_new = max(anchor_dt.date(), today_ist())
    ok, err = trip_service_inst.create_trip_attendance_records(
        session,
        trip_id,
        start_date=start_for_new,
        end_date=new_end_date,
        trip_start_dt=anchor_dt,
        trip_duration_hours=duration_hours,
        selected_days=new_selected_days,
        single_shift=False,
    )
    if not ok:
        raise HTTPException(400, err or "Failed to regenerate attendance rows")

    trip.selected_days = new_selected_days
    trip.end_date = new_end_date
    trip.fare = new_fare
    trip.fare_breakdown = fare_quote
    trip.state_version += 1
    session.add(trip)

    diff_bill_id = None
    if fare_diff > 0:
        bill = TripBill(
            trip_id=trip_id,
            user_id=trip.user_id,
            driver_id=trip.driver_id or 0,
            bill_type="schedule_diff",
            bill_date=_date.today(),
            total_amount=fare_diff,
            amount_paid=0.0,
            amount_due=fare_diff,
            is_generated=True,
            is_paid=False,
            components=[
                {
                    "name": "Schedule modification diff",
                    "amount": fare_diff,
                    "old_fare": old_fare,
                    "new_fare": new_fare,
                }
            ],
            notes=f"Schedule modified: extended/adjusted to {new_end_date}",
        )
        session.add(bill)
        session.flush()
        diff_bill_id = bill.id
    elif fare_diff < 0:
        session.add(
            Payment(
                reference_id=generate_reference_id(session, PAYMENT),
                service_type="trip",
                service_reference_id=trip.reference_id,
                service_id=trip.id,
                user_id=trip.user_id,
                payer_type="user",
                purpose="schedule_diff",
                amount=abs(fare_diff),
                channel="credit",
                status="succeeded",
                completed_at=now_ist(),
                extra={
                    "reason": (
                        f"Schedule modified, fare reduced by ₹{abs(fare_diff):.2f}"
                    )
                },
            )
        )

    session.commit()

    return {
        "message": "Schedule modified",
        "trip_id": trip.reference_id,
        "old_fare": old_fare,
        "new_fare": new_fare,
        "fare_diff": fare_diff,
        "schedule_diff_bill_id": diff_bill_id,
        "selected_days": new_selected_days,
        "end_date": new_end_date.isoformat() if new_end_date else None,
    }


@router.post("/check-escalation")
def check_and_escalate_tiers(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    count = process_tier_escalation(session)
    return {"message": f"Escalated {count} trips."}


@router.post("/{trip_id}/select-payment-method")
def select_payment_method(
    trip_id: str,
    payment_method: str = Body(..., embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Select or change the trip's payment method."""
    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Not authorized")

    if payment_method not in ("trip_day", "advance_20", "full_payment"):
        raise HTTPException(400, "Invalid payment method")

    if (trip.hiring_type or "").strip().lower() == "outstation":
        raise HTTPException(
            400,
            "Outstation trips are billed once at trip end (3% discount) — "
            "the payment method cannot be selected or changed.",
        )

    current = trip.payment_method

    upfront_pending = bool(trip.is_payment_blocked) and current in (
        "advance_20",
        "full_payment",
    )

    if current is None:
        allowed = {"trip_day", "advance_20", "full_payment"}
    elif current == "trip_day":
        allowed = {"advance_20", "full_payment"}
    elif current == "advance_20":
        allowed = {"trip_day", "full_payment"} if upfront_pending else {"full_payment"}
    else:
        allowed = {"trip_day", "advance_20"} if upfront_pending else set()

    if payment_method == current:
        raise HTTPException(400, f"Payment method is already '{current}'.")
    if payment_method not in allowed:
        if current == "full_payment":
            raise HTTPException(
                400,
                "Full payment is already settled upfront — the payment method "
                "cannot be changed.",
            )
        if payment_method == "trip_day":
            raise HTTPException(
                400,
                "Cannot switch back to trip-day billing once advance_20 / "
                "full_payment has been chosen.",
            )
        raise HTTPException(
            400,
            f"Cannot change payment method from '{current}' to '{payment_method}'.",
        )

    if trip.status in ("ongoing", "active"):
        raise HTTPException(
            400,
            "A shift is currently in progress. Change the payment method "
            "between shifts.",
        )
    CHANGE_ALLOWED_STATES = (
        "searching",
        "accepted_pending_payment",
        "payment_in_progress",
        "active_pending_otp",
        "paused",
    )
    if trip.status not in CHANGE_ALLOWED_STATES:
        raise HTTPException(
            400,
            f"Payment method cannot be changed while the trip is '{trip.status}'.",
        )

    trip_service = TripService()

    attendance_count = len(
        session.exec(
            select(TripAttendance.id).where(TripAttendance.trip_id == trip_id)
        ).all()
    )
    if attendance_count > 0 and not trip_service.has_pending_shifts(session, trip_id):
        raise HTTPException(
            400,
            "All shifts are completed — settle the final bill with the current "
            "payment method.",
        )

    if payment_method == "trip_day":
        trip.payment_method = "trip_day"
        trip.state_version += 1
        session.add(trip)
        session.commit()
        PaymentService(None).unpause_trip_if_clear(session, trip_id)
        session.commit()
        session.refresh(trip)
        return {
            "message": "Payment method set to trip_day",
            "trip_status": trip.status,
        }

    if PaymentService(None).trip_has_unpaid_bills(session, trip_id):
        raise HTTPException(
            400,
            "You have an unpaid bill on this trip. Please clear all outstanding "
            "bills before switching to advance_20 / full_payment.",
        )

    portion = trip_service.compute_outstanding_portion(session, trip)
    outstanding_gross = portion["outstanding_gross"]
    if outstanding_gross <= 0:
        raise HTTPException(
            400, "Nothing left to bill — the payment method cannot be changed."
        )

    if payment_method == "advance_20":
        upfront = round(0.20 * outstanding_gross, 2)
        unsettled = portion["unsettled_bills"]
        if unsettled:
            most_recent_due = unsettled[-1].amount_due or 0.0
            if upfront < most_recent_due:
                raise HTTPException(
                    400,
                    f"advance_20 not allowed here: the 20% upfront "
                    f"(₹{upfront:.2f}) is less than the pending shift bill "
                    f"(₹{most_recent_due:.2f}). Pay the pending bill or choose "
                    f"full_payment.",
                )

    amount_due = max(
        0.0,
        round(
            (0.20 if payment_method == "advance_20" else 0.95) * outstanding_gross
            - portion["payments_applied"],
            2,
        ),
    )

    trip.payment_method = payment_method
    trip.is_payment_blocked = True
    if trip.status == "active_pending_otp":
        trip.status = "paused"
    trip.state_version += 1
    session.add(trip)
    session.commit()

    return {
        "message": f"Payment method changed to {payment_method}",
        "next_step": "upfront_payment_required",
        "amount_due": amount_due,
        "upfront_amount_due": amount_due,
        "full_fare": trip.fare,
        "trip_status": trip.status,
    }


@router.post("/{trip_id}/pay-upfront")
def user_pay_upfront(
    trip_id: str,
    background_tasks: BackgroundTasks,
    channel: str = Body("platform", embed=True),
    card_reference_id: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    idem: IdempotencyGuard = Depends(idempotent("upfront.pay", ["trip_id"])),
):
    """User upfront payment for an advance_20 / full_payment trip."""
    if idem.cached_response is not None:
        return idem.cached_response
    _validate_online_channel(channel)
    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.user_id != current_user.id:
        raise HTTPException(403, "Not authorized")
    if trip.payment_method not in ("advance_20", "full_payment"):
        raise HTTPException(
            400,
            "Upfront payment applies only to advance_20 / full_payment trips.",
        )
    if not trip.fare or trip.fare <= 0:
        raise HTTPException(400, "Trip fare not calculated yet")
    if trip.status in ("ongoing", "active"):
        raise HTTPException(
            400, "A shift is in progress — complete it before paying upfront."
        )

    trip_service = TripService()
    portion = trip_service.compute_outstanding_portion(session, trip)
    outstanding_gross = portion["outstanding_gross"]
    rate = 0.95 if trip.payment_method == "full_payment" else 0.20
    amount = max(0.0, round(rate * outstanding_gross - portion["payments_applied"], 2))

    payment_service = PaymentService(redis_client)

    if amount > 0:
        payment, client_secret, err = payment_service.user_make_payment(
            session,
            trip,
            current_user,
            amount,
            channel=channel,
            card_reference_id=card_reference_id,
            idempotency_key=idempotency_key,
        )
        if err:
            raise HTTPException(400, err)
        if client_secret:
            _maybe_schedule_platform_settlement(background_tasks, payment)
            response = {
                "message": "Upfront payment initiated. Confirm with the client "
                "secret; the trip unlocks once the gateway confirms.",
                "trip_id": trip.reference_id,
                "amount": amount,
                "payment_method": trip.payment_method,
                "channel": channel,
                "status": "pending",
                "payment_reference": payment.reference_id,
                "client_secret": client_secret,
            }
            idem.store(response)
            return response
    else:
        from app.modules.trips import payment_orchestrator

        payment_orchestrator.apply_user_upfront_settlement(session, trip)
        session.commit()

    session.refresh(trip)
    response = {
        "message": "Upfront payment successful",
        "trip_id": trip.reference_id,
        "amount_paid": amount,
        "payment_method": trip.payment_method,
        "trip_status": trip.status,
    }
    idem.store(response)
    return response


@router.post("/driver/{trip_id}/accept-and-pay")
def driver_accept_and_initiate_payment(
    trip_id: str,
    action: str = Body(..., embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id).with_for_update()
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can perform this action")

    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_id and trip.driver_id != driver.id:
        raise HTTPException(400, "Trip already accepted by another driver")

    if action == "reject":
        offer = session.exec(
            select(TripOffer)
            .where(
                TripOffer.trip_id == trip_id,
                TripOffer.driver_id == driver.id,
            )
            .with_for_update()
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

    elif action == "accept":
        if trip.status not in ("searching", "accepted_pending_payment"):
            raise HTTPException(
                400,
                f"Trip cannot be accepted in status '{trip.status}'.",
            )

        busy_trip = session.exec(
            select(Trip).where(
                Trip.driver_id == driver.id,
                Trip.id != trip_id,
                Trip.status.in_(TripService.DRIVER_BUSY_STATES),
            )
        ).first()
        if busy_trip:
            raise HTTPException(
                409,
                f"You already have an active trip (id={busy_trip.reference_id}, "
                f"status={busy_trip.status}). Complete it before accepting another.",
            )

        accepted_offer = session.exec(
            select(TripOffer)
            .where(
                TripOffer.trip_id == trip_id,
                TripOffer.driver_id == driver.id,
                TripOffer.status == "pending",
            )
            .with_for_update()
        ).first()
        if not accepted_offer:
            raise HTTPException(403, "No active offer for this trip for this driver")

        trip.driver_id = driver.id
        trip.status = "accepted_pending_payment"
        trip.driver_accepted_at = now_ist()

        accepted_offer.status = "accepted"
        session.add(accepted_offer)

        sibling_offers = session.exec(
            select(TripOffer)
            .where(
                TripOffer.trip_id == trip_id,
                TripOffer.driver_id != driver.id,
                TripOffer.status == "pending",
            )
            .with_for_update()
        ).all()
        for o in sibling_offers:
            session.delete(o)

        redis_key = f"driver_payment_timer:{trip_id}:{driver.id}"
        redis_client.setex(redis_key, 1800, "pending")

        session.add(trip)
        session.commit()

        redis_client.delete(f"driver_{driver.id}")

        return {
            "message": "Trip accepted. Payment required.",
            "trip_id": trip.reference_id,
            "payment_required": get_driver_acceptance_fee(session, redis_client),
            "timer_seconds": 1800,
            "payment_deadline": (now_ist() + timedelta(minutes=30)).isoformat(),
        }

    else:
        raise HTTPException(400, "Invalid action. Use 'accept' or 'reject'")


@router.post("/driver/{trip_id}/process-payment")
def driver_process_payment(
    trip_id: str,
    background_tasks: BackgroundTasks,
    channel: str = Body("platform", embed=True),
    card_reference_id: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    idem: IdempotencyGuard = Depends(idempotent("driver.process_payment", ["trip_id"])),
):
    """Driver pays the acceptance fee to lock the trip."""
    if idem.cached_response is not None:
        return idem.cached_response
    _validate_driver_channel(channel)
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can perform this action")

    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.driver_id != driver.id:
        raise HTTPException(403, "Not authorized")
    if trip.status != "accepted_pending_payment":
        raise HTTPException(
            400, f"Trip status is {trip.status}, cannot process payment now"
        )

    if not trip.payment_method:
        trip.payment_method = "trip_day"
        session.add(trip)
        session.flush()

    payment_service = PaymentService(redis_client)
    payment, client_secret, error = payment_service.driver_accept_payment(
        session,
        trip.id,
        driver.id,
        channel=channel,
        card_reference_id=card_reference_id,
        idempotency_key=idempotency_key,
    )
    if error:
        raise HTTPException(400, f"Payment failed: {error}")

    try:
        redis_client.delete(f"driver_payment_timer:{trip.id}:{driver.id}")
    except Exception:
        pass
    _maybe_schedule_platform_settlement(background_tasks, payment)
    response = {
        "message": "Driver payment initiated. Confirm with the client secret; "
        "the trip arms once the gateway confirms.",
        "trip_id": trip.reference_id,
        "trip_status": trip.status,
        "channel": channel,
        "status": "pending",
        "payment_reference": payment.reference_id,
        "client_secret": client_secret,
        "next_step": "confirm_gateway",
    }
    idem.store(response)
    return response


@router.post("/driver/{trip_id}/withdraw")
def driver_withdraw_from_trip(
    trip_id: str,
    reason: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Driver backs out of an accepted trip BEFORE any shift has started (F5)."""
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id).with_for_update()
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can withdraw")

    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.driver_id != driver.id:
        raise HTTPException(403, "You are not the assigned driver on this trip")
    if trip.status not in (
        "accepted_pending_payment",
        "active_pending_otp",
        "paused",
    ):
        raise HTTPException(
            400,
            f"Withdraw not allowed from status '{trip.status}'. "
            "Use the in-shift skip flow instead.",
        )

    served = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status == "present",
        )
    ).first()
    if served:
        raise HTTPException(
            403,
            "You have already served at least one shift on this booking. "
            "Use the abandon flow instead — withdraw is only for the period "
            "before the first shift starts.",
        )

    first_att = session.exec(
        select(TripAttendance)
        .where(TripAttendance.trip_id == trip_id)
        .order_by(TripAttendance.scheduled_start)
        .limit(1)
    ).first()

    eligible_for_refund = False
    if first_att and first_att.scheduled_start:
        five_h_before = first_att.scheduled_start - timedelta(hours=5)
        eligible_for_refund = now_ist() < five_h_before
    else:
        eligible_for_refund = True

    payment_service = PaymentService(redis_client)
    refunded_amount = 0.0
    if eligible_for_refund and trip.driver_payment_status == "paid":
        ok, err = payment_service.refund_driver_acceptance_fee(
            session,
            trip_id,
            driver.id,
            reason=reason or "Driver withdrew >5h before first shift",
        )
        if not ok:
            raise HTTPException(400, err or "Refund failed")
        refunded_amount = trip.driver_payment_amount or get_driver_acceptance_fee(
            session, redis_client
        )
        trip.driver_payment_status = "unpaid"
        trip.driver_payment_amount = None

    own_offer = session.exec(
        select(TripOffer)
        .where(TripOffer.trip_id == trip_id, TripOffer.driver_id == driver.id)
        .with_for_update()
    ).first()
    if own_offer and own_offer.status in ("accepted", "pending"):
        own_offer.status = "rejected"
        session.add(own_offer)

    trip.driver_id = None
    trip.driver_accepted_at = None
    trip_service_inst = TripService()
    success, terr = trip_service_inst.transition_trip_state(
        session, trip_id, "searching", validate=False
    )
    if not success:
        raise HTTPException(400, terr or "State transition failed")

    session.add(trip)
    session.commit()

    try:
        redis_client.delete(f"driver_{driver.id}")
    except Exception:
        pass

    try:
        attempt_trip_escalation(session, trip)
        session.commit()
    except Exception:
        pass

    try:
        send_push_notification(
            session=session,
            user_ids=[trip.user_id],
            title="Driver unavailable",
            body="Your assigned driver had to step away — we're finding a replacement now.",
            data={"type": "driver_withdrew", "trip_id": trip.reference_id},
        )
    except Exception:
        pass

    return {
        "message": "Withdrew from trip.",
        "trip_id": trip.reference_id,
        "trip_status": "searching",
        "refund_amount": refunded_amount,
        "refund_eligible": eligible_for_refund,
    }


@router.post("/driver/{trip_id}/abandon")
def driver_abandon_trip(
    trip_id: str,
    reason: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Driver permanently exits a multi-day booking after serving ≥1 shift (F11)."""
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id).with_for_update()
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can abandon a trip")

    trip = session.exec(
        select(Trip).where(Trip.reference_id == trip_id).with_for_update()
    ).first()
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")
    if trip.driver_id != driver.id:
        raise HTTPException(403, "You are not the assigned driver on this trip")

    if trip.status not in ("active_pending_otp", "active", "ongoing", "paused"):
        raise HTTPException(
            400,
            f"Abandon not allowed from status '{trip.status}'. "
            "Use /withdraw before the first shift starts.",
        )

    served = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status == "present",
        )
    ).first()
    if not served:
        raise HTTPException(
            400,
            "No shift has been served yet. Use /withdraw instead of /abandon.",
        )

    payment_service = PaymentService(redis_client)
    refund_amount = payment_service.calculate_driver_abandon_refund(session, trip_id)

    pending_atts = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
    ).all()
    for a in pending_atts:
        a.status = "skipped_by_system"
        a.skip_reason = reason or "Driver abandoned booking"
        a.marked_by = "system"
        session.add(a)

    trip_service_inst = TripService()
    ok, err = trip_service_inst.transition_trip_state(
        session, trip_id, "cancelled_by_driver", validate=False
    )
    if not ok:
        raise HTTPException(400, err or "State transition failed")

    if refund_amount > 0:
        refund_ok, refund_err = payment_service.process_refund(
            session,
            trip_id,
            refund_amount,
            reason=reason or "Driver abandoned booking",
        )
        if not refund_ok:
            raise HTTPException(502, refund_err or "Refund gateway error")

        trip_service_inst.transition_trip_state(
            session, trip_id, "refund_processing", validate=False
        )
        trip_service_inst.transition_trip_state(
            session, trip_id, "settled", validate=False
        )
    else:
        trip_service_inst.transition_trip_state(
            session, trip_id, "refund_processing", validate=False
        )
        trip_service_inst.transition_trip_state(
            session, trip_id, "settled", validate=False
        )

    suspension_hours = get_driver_abandon_suspension_hours(session, redis_client)
    driver.suspended_until = now_ist() + timedelta(hours=suspension_hours)
    session.add(driver)
    session.commit()

    try:
        redis_client.delete(f"driver_{driver.id}")
    except Exception:
        pass

    try:
        send_push_notification(
            session=session,
            user_ids=[trip.user_id],
            title="Driver had to step away",
            body=(
                f"Your driver couldn't continue this booking. ₹{refund_amount:.2f} "
                "is being refunded for the unused days."
                if refund_amount > 0
                else "Your driver couldn't continue this booking. Past days are already paid."
            ),
            data={
                "type": "driver_abandoned",
                "trip_id": trip.reference_id,
                "refund_amount": refund_amount,
            },
        )
    except Exception:
        pass

    return {
        "message": "Trip closed — driver abandoned.",
        "trip_id": trip.reference_id,
        "trip_status": "settled",
        "refund_amount": refund_amount,
    }


@router.post("/{trip_id}/request-otp")
def request_otp_for_trip(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can fetch the OTP")

    if trip.status != "active_pending_otp":
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
        if PaymentService(None).trip_has_unpaid_bills(session, trip.id):
            raise HTTPException(
                400,
                "Trip is paused — settle the outstanding bill before requesting today's OTP.",
            )
        if trip.payment_method in ("advance_20", "full_payment"):
            raise HTTPException(
                400,
                f"Trip is paused — complete {trip.payment_method} upfront payment before requesting OTP.",
            )
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
    if now > attendance.scheduled_start + timedelta(hours=12):
        raise HTTPException(
            400,
            "OTP window for this shift has already expired.",
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
            data={"type": "trip_otp", "trip_id": trip.reference_id},
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
    trip_id: str,
    otp: str = Body(..., embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only the assigned driver can verify the OTP")

    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
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
        session, trip_id, driver.id, otp, trip_date=shift_date
    )
    if not is_valid:
        raise HTTPException(400, error)

    trip_service = TripService()
    trip_service.transition_trip_state(session, trip_id, "active", validate=False)
    trip_service.transition_trip_state(session, trip_id, "ongoing", validate=False)

    verify_now = now_ist()
    if trip.actual_start_time is None:
        trip.actual_start_time = verify_now

    valid_attendance.user_otp_verified = True
    valid_attendance.driver_otp_verified = True
    valid_attendance.actual_start = verify_now
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
            data={"type": "trip_started", "trip_id": trip.reference_id},
        )
    except Exception:
        pass

    return {
        "message": "OTP verified. Trip is now ongoing!",
        "trip_id": trip.reference_id,
        "trip_status": "ongoing",
    }


@router.post("/{trip_id}/skip-day")
def skip_trip_day(
    trip_id: str,
    body: TripDaySkipRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip_date = body.trip_date
    reason = body.reason

    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
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

    if trip.is_payment_blocked or trip.status == "paused":
        raise HTTPException(
            400,
            "Trip is paused for a pending payment. Settle the outstanding "
            "amount before skipping a day.",
        )

    if (trip.hiring_type or "").strip().lower() == "outstation":
        raise HTTPException(
            400,
            "Outstation trips cannot be skipped. Use the cancel option to drop this booking.",
        )

    today = today_ist()

    if trip_date < today:
        raise HTTPException(400, "Cannot skip a past trip day.")

    if trip_date != today:
        raise HTTPException(
            400,
            "Skip is only allowed for today's shift. To remove a future day, cancel the trip.",
        )

    if not is_user and trip.status == "ongoing":
        raise HTTPException(
            400,
            "Trip is already in progress. End the trip instead of skipping the day.",
        )

    trip_service = TripService()
    success, error = trip_service.mark_trip_day_absent(
        session, trip_id, trip_date, reason, marked_by
    )

    if not success:
        raise HTTPException(400, error)

    billing_service = BillingService()
    try:
        billing_service.check_advance_recovery(session, trip_id)
    except Exception:
        pass

    settlement_id = None
    pending = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
    ).first()
    if not pending:
        ok, sid, _ = billing_service.generate_final_settlement(session, trip_id)
        if ok or sid:
            settlement_id = sid

    return {
        "message": f"Day {trip_date} marked as absent by {marked_by}",
        "trip_id": trip.reference_id,
        "trip_date": trip_date.isoformat(),
        "settlement_id": settlement_id,
    }


@router.post("/{trip_id}/end-trip")
def end_trip(
    trip_id: str,
    notes: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """End the current shift — USER-ONLY."""
    _ = notes

    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can end the trip")

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

    end_now = now_ist()
    if not has_future_shifts:
        trip.actual_end_time = end_now
    else:
        trip.scheduled_start_time = next_attendance.scheduled_start
        trip.scheduled_end_time = next_attendance.scheduled_end

    session.add(trip)
    session.commit()

    trip_service.mark_trip_day_present(session, trip_id, shift_date, actual_end=end_now)

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
                    "trip_id": trip.reference_id,
                    "bill_id": bill_id,
                    "amount": amount,
                },
            )
        except Exception:
            pass

    try:
        billing_service.check_advance_recovery(session, trip_id)
    except Exception:
        pass

    if has_future_shifts:
        payment_service = PaymentService(redis_client)
        if payment_service.trip_has_unpaid_bills(session, trip_id):
            trip.status = "paused"
            trip.is_payment_blocked = True
            trip.state_version += 1
            session.add(trip)
            session.commit()

    settlement_id = None
    if not has_future_shifts:
        ok, sid, _ = billing_service.generate_final_settlement(session, trip_id)
        if ok or sid:
            settlement_id = sid
            try:
                settlement = session.get(TripSettlement, sid) if sid else None
                if settlement and settlement.remaining_due > 0:
                    send_push_notification(
                        session=session,
                        user_ids=[trip.user_id],
                        title="Final settlement ready",
                        body=(
                            f"Trip #{trip_id} settlement is ready. "
                            f"Amount due: ₹{settlement.remaining_due:.2f}."
                        ),
                        data={
                            "type": "settlement_generated",
                            "trip_id": trip.reference_id,
                            "settlement_id": sid,
                            "amount": settlement.remaining_due,
                        },
                    )
            except Exception:
                pass

    return {
        "message": "Trip ended successfully",
        "trip_id": trip.reference_id,
        "actual_end_time": trip.actual_end_time.isoformat()
        if trip.actual_end_time
        else None,
        "trip_status": trip.status,
        "more_days_remaining": has_future_shifts,
        "bill_generated": bill_success,
        "bill_id": bill_id,
        "settlement_id": settlement_id,
    }


@router.get("/{trip_id}/bill/{bill_id}")
def get_trip_bill(
    trip_id: str,
    bill_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = bool(driver and trip.driver_id == driver.id)

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    bill_row = session.get(TripBill, bill_id)
    if not bill_row or bill_row.trip_id != trip_id:
        raise HTTPException(404, "Bill not found")

    billing_service = BillingService()
    bill_details = billing_service.get_bill_details(session, bill_id)

    if not bill_details:
        raise HTTPException(404, "Bill not found")

    bill_details["trip_id"] = trip.reference_id
    return bill_details


@router.get("/{trip_id}/settlement")
def get_trip_settlement(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = bool(driver and trip.driver_id == driver.id)

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    settlement = session.exec(
        select(TripSettlement).where(TripSettlement.trip_id == trip_id)
    ).first()

    if not settlement:
        raise HTTPException(404, "Settlement not generated yet")

    billing_service = BillingService()
    settlement_details = billing_service.get_settlement_details(session, settlement.id)

    if settlement_details:
        settlement_details["trip_id"] = trip.reference_id
    return settlement_details


@router.get("/{trip_id}/summary")
def get_trip_summary(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_user = trip.user_id == current_user.id
    is_driver = False

    if not is_user:
        driver = session.exec(
            select(Driver).where(Driver.user_id == current_user.id)
        ).first()
        is_driver = bool(driver and trip.driver_id == driver.id)

    if not is_user and not is_driver:
        raise HTTPException(403, "Not authorized")

    trip_service = TripService()
    summary = trip_service.get_trip_summary(session, trip_id, is_driver)

    if not summary:
        raise HTTPException(500, "Could not generate trip summary")

    return summary


@router.get("/{trip_id}/bills", response_model=List[TripBillRead])
def list_trip_bills(
    trip_id: str,
    only_unpaid: bool = False,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = get_by_reference(session, Trip, trip_id)
    trip_id = trip.id if trip else trip_id
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
        TripBill.bill_type.in_(("daily_bill", "advance_recovery")),
    )
    if only_unpaid:
        stmt = stmt.where(TripBill.amount_due > 0)
    rows = session.exec(stmt.order_by(TripBill.bill_date)).all()
    return [
        TripBillRead(
            **{
                **row.model_dump(exclude={"trip_id"}),
                "trip_id": trip.reference_id,
            }
        )
        for row in rows
    ]


@router.post("/bill/{bill_id}/pay")
def user_pay_bill(
    bill_id: int,
    background_tasks: BackgroundTasks,
    channel: str = Body("platform", embed=True),
    card_reference_id: Optional[str] = Body(None, embed=True),
    note: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    idem: IdempotencyGuard = Depends(idempotent("bill.pay", ["bill_id"])),
):
    if idem.cached_response is not None:
        return idem.cached_response
    _validate_online_channel(channel)
    bill = session.get(TripBill, bill_id)
    if not bill:
        raise HTTPException(404, "Bill not found")
    if bill.user_id != current_user.id:
        raise HTTPException(403, "Not authorized for this bill")

    trip = session.get(Trip, bill.trip_id)
    if (
        trip
        and trip.payment_method != "trip_day"
        and bill.bill_type not in ("cancellation_balance", "advance_recovery")
    ):
        raise HTTPException(
            400,
            f"Daily bills cannot be paid individually for the '{trip.payment_method}' payment method. Please pay the final settlement at the end of the trip.",
        )

    bill_amount = bill.total_amount
    payment_service = PaymentService(redis_client)
    payment, client_secret, err = payment_service.pay_bill_online(
        session,
        bill_id,
        current_user,
        channel=channel,
        card_reference_id=card_reference_id,
        idempotency_key=idempotency_key,
        note=note,
    )
    if err:
        raise HTTPException(400, err)

    if client_secret:
        _maybe_schedule_platform_settlement(background_tasks, payment)
        response = {
            "message": "Bill payment initiated. Confirm with the client secret; "
            "the bill clears once the gateway confirms.",
            "bill_id": bill_id,
            "trip_id": (trip.reference_id if trip else None),
            "amount": bill_amount,
            "channel": channel,
            "status": "pending",
            "payment_reference": payment.reference_id,
            "client_secret": client_secret,
        }
        idem.store(response)
        return response

    try:
        send_push_notification(
            session=session,
            user_ids=[bill.user_id],
            title="Bill paid",
            body=f"Bill #{bill_id} of ₹{bill_amount:.2f} settled. Thank you!",
            data={
                "type": "bill_paid",
                "bill_id": bill_id,
                "trip_id": (trip.reference_id if trip else None),
            },
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
                body=f"Bill #{bill_id} of ₹{bill_amount:.2f} paid by user.",
                data={
                    "type": "bill_paid",
                    "bill_id": bill_id,
                    "trip_id": (trip.reference_id if trip else None),
                },
            )
    except Exception:
        pass

    response = {
        "message": "Bill paid successfully",
        "bill_id": bill_id,
        "trip_id": (trip.reference_id if trip else None),
        "amount": bill_amount,
    }
    idem.store(response)
    return response


@router.post("/bill/{bill_id}/mark-paid-by-driver")
def driver_mark_bill_paid(
    bill_id: int,
    note: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    idem: IdempotencyGuard = Depends(
        idempotent("bill.mark_paid_by_driver", ["bill_id"])
    ),
):
    if idem.cached_response is not None:
        return idem.cached_response
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
    if (
        trip
        and trip.payment_method != "trip_day"
        and bill.bill_type != "advance_recovery"
    ):
        raise HTTPException(
            400,
            f"Daily bills cannot be paid individually for the '{trip.payment_method}' payment method.",
        )

    payment_service = PaymentService(redis_client)
    ok, err = payment_service.mark_bill_paid_offline(session, bill_id, driver.id, note)
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
                "trip_id": (trip.reference_id if trip else None),
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
                    "trip_id": (trip.reference_id if trip else None),
                },
            )
    except Exception:
        pass

    response = {
        "message": "Bill marked as paid by driver",
        "bill_id": bill_id,
        "trip_id": (trip.reference_id if trip else None),
        "amount": bill.total_amount,
        "paid_by": "driver_offline",
    }
    idem.store(response)
    return response


_ALLOWED_EXTRA_KEYS = {"toll", "parking", "food", "other"}


@router.post("/settlement/{settlement_id}/pay")
def pay_trip_settlement(
    settlement_id: int,
    background_tasks: BackgroundTasks,
    channel: str = Body("platform", embed=True),
    card_reference_id: Optional[str] = Body(None, embed=True),
    note: Optional[str] = Body(None, embed=True),
    extra_amount: Optional[float] = Body(None, embed=True),
    extra_amount_breakdown: Optional[dict] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    idem: IdempotencyGuard = Depends(idempotent("settlement.pay", ["settlement_id"])),
):
    """User pays the final trip settlement for advance_20 or full_payment methods."""
    if idem.cached_response is not None:
        return idem.cached_response
    _validate_online_channel(channel)
    settlement = session.exec(
        select(TripSettlement)
        .where(TripSettlement.id == settlement_id)
        .with_for_update()
    ).first()
    if not settlement:
        raise HTTPException(404, "Settlement not found")

    if settlement.user_id != current_user.id:
        raise HTTPException(403, "Not authorized for this settlement")

    if settlement.user_payment_status == "paid":
        raise HTTPException(400, "Settlement is already paid")

    if settlement.remaining_due <= 0:
        raise HTTPException(400, "No remaining amount due")

    trip = session.get(Trip, settlement.trip_id)
    is_outstation = (
        trip is not None and (trip.hiring_type or "").strip().lower() == "outstation"
    )

    extra = float(extra_amount or 0.0)
    if extra < 0:
        raise HTTPException(400, "extra_amount must be non-negative")
    if extra > 0 and not is_outstation:
        raise HTTPException(
            400,
            "extra_amount is only allowed for outstation trip settlements",
        )

    validated_breakdown: Optional[dict] = None
    if extra > 0:
        if not extra_amount_breakdown or not isinstance(extra_amount_breakdown, dict):
            raise HTTPException(
                400,
                "extra_amount_breakdown is required when extra_amount > 0. "
                f"Keys must be a subset of {sorted(_ALLOWED_EXTRA_KEYS)}.",
            )
        unknown = set(extra_amount_breakdown.keys()) - _ALLOWED_EXTRA_KEYS
        if unknown:
            raise HTTPException(
                400,
                f"Unknown extra_amount_breakdown keys: {sorted(unknown)}. "
                f"Allowed: {sorted(_ALLOWED_EXTRA_KEYS)}.",
            )
        cleaned: dict = {}
        for k, v in extra_amount_breakdown.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                raise HTTPException(
                    400, f"extra_amount_breakdown[{k!r}] must be a number"
                )
            if fv < 0:
                raise HTTPException(
                    400, f"extra_amount_breakdown[{k!r}] must be non-negative"
                )
            if fv > 0:
                cleaned[k] = round(fv, 2)
        breakdown_sum = round(sum(cleaned.values()), 2)
        if abs(breakdown_sum - round(extra, 2)) > 1.0:
            raise HTTPException(
                400,
                f"extra_amount_breakdown sums to ₹{breakdown_sum:.2f} "
                f"but extra_amount is ₹{extra:.2f} (must match within ₹1).",
            )
        validated_breakdown = cleaned

    total_charge = round(settlement.remaining_due + extra, 2)

    if not trip:
        raise HTTPException(404, "Trip not found for settlement")

    extra_meta: dict = {"settlement_id": settlement.id}
    if note:
        extra_meta["note"] = note
    if extra > 0:
        extra_meta["extra_amount"] = round(extra, 2)
        extra_meta["extra_amount_breakdown"] = validated_breakdown or {}

    payment, client_secret = central_payments.create_trip_payment_intent(
        session,
        trip=trip,
        purpose="settlement",
        amount=total_charge,
        payer_type="user",
        payer_user=current_user,
        channel=channel,
        card_reference_id=card_reference_id,
        idempotency_key=idempotency_key,
        extra=extra_meta,
    )

    if client_secret:
        _maybe_schedule_platform_settlement(background_tasks, payment)
        response = {
            "message": "Settlement payment initiated. Confirm with the client "
            "secret; it clears once the gateway confirms.",
            "settlement_id": settlement.id,
            "trip_id": trip.reference_id,
            "amount_paid": total_charge,
            "extra_amount_paid": round(extra, 2),
            "channel": channel,
            "status": "pending",
            "payment_reference": payment.reference_id,
            "client_secret": client_secret,
        }
        idem.store(response)
        return response

    session.refresh(settlement)

    try:
        send_push_notification(
            session=session,
            user_ids=[settlement.user_id],
            title="Settlement Paid",
            body=f"Your final settlement of ₹{total_charge:.2f} has been paid successfully.",
            data={
                "type": "settlement_paid",
                "trip_id": trip.reference_id,
            },
        )
    except Exception:
        pass

    response = {
        "message": "Settlement paid successfully",
        "settlement_id": settlement.id,
        "trip_id": trip.reference_id,
        "amount_paid": total_charge,
        "extra_amount_paid": round(extra, 2),
        "extra_amount_breakdown": settlement.extra_amount_breakdown,
    }
    idem.store(response)
    return response
