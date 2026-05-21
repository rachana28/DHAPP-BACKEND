import redis
from datetime import datetime, timedelta
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import List, Optional
from sqlalchemy.orm import selectinload

from app.core.database import get_session, get_redis
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
from app.modules.trips.billing_service import (
    BillingService,
    payment_method_discount_pct,
)
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

    trip_data["driver_id"] = None
    trip_data["tow_truck_driver_id"] = None

    # (Daily / Monthly / Outstation).
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


@router.get("/my-bookings")
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
        trips = session.exec(
            select(Trip)
            .where(Trip.driver_id == driver.id)
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.user))
        ).all()
        # Skip allowance is scoped per trip booking — surface each trip's own
        # remaining count.
        trip_service = TripService()
        # Hand-marshal so we never leak the user UUID or internal flags.
        return [
            TripReadDriver(
                **{
                    k: getattr(t, k)
                    for k in TripReadDriver.model_fields.keys()
                    if k not in ("user", "driver_skips_remaining")
                },
                user={
                    "full_name": t.user.full_name if t.user else None,
                    "phone_number": t.user.phone_number if t.user else None,
                    "avatar_url": getattr(t.user, "avatar_url", None)
                    if t.user
                    else None,
                }
                if t.user
                else None,
                driver_skips_remaining=trip_service.driver_skips_remaining(
                    session, t.id
                ),
            )
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
        statement = (
            select(Trip)
            .where(Trip.user_id == current_user.id)
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.driver))
        )
        trips = session.exec(statement).all()

        # Build a Pydantic view per trip so mutating `driver`/`driver_id`
        # on the in-memory ORM row can never accidentally persist.
        trip_service = TripService()
        result = []
        for t in trips:
            view = TripReadUser.model_validate(t, from_attributes=True)
            if t.status in DRIVER_HIDDEN_STATES:
                view.driver = None
            elif t.driver_id is not None:
                # Surface the assigned driver's per-trip skip allowance for
                # transparency, only once the driver is visible to the user.
                view.driver_skips_remaining = trip_service.driver_skips_remaining(
                    session, t.id
                )
            result.append(view)
        return result
    else:
        return []


@router.post("/{trip_id}/cancel")
def cancel_trip(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """User-initiated cancellation.

    Drivers may NOT cancel — they reject offers via /accept-and-pay action="reject".

    Payment-method-aware rules (also encoded in TripService.can_cancel_trip):

      * trip_day:
          - allowed in any non-terminal, non-ongoing state with no unpaid bill
          - completed shifts are already paid for → NO user refund
          - future scheduled shifts are voided
          - if any shift was completed: trip closes as `completed` and a final
            settlement is generated to wrap up; bills already paid stay paid
          - if no shift was completed: trip closes as `cancelled_by_user` and
            the driver acceptance fee is refunded

      * advance_20 / full_payment:
          - allowed only BEFORE any shift has started
          - any pre-trip user payment is fully refunded
          - the driver acceptance fee is refunded
          - trip closes as `cancelled_by_user`

      * ongoing / active: blocked — wait for the shift to end
      * paused / unpaid bill: blocked — pay the outstanding bill first
    """
    # Row-lock the trip so this can't interleave with driver-side OTP/end-trip.
    trip = session.exec(
        select(Trip).where(Trip.id == trip_id).with_for_update()
    ).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    # User-only flow.
    if trip.user_id != current_user.id:
        raise HTTPException(403, "Only the trip's user can cancel this booking")

    trip_service = TripService()
    can_cancel, reason, has_completed_shift = trip_service.can_cancel_trip(
        session,
        trip_id,
        user_id=str(current_user.id),
    )
    if not can_cancel:
        raise HTTPException(400, reason or "Trip cannot be cancelled")

    payment_service = PaymentService(redis_client)

    # trip_day: any outstanding daily bill must be settled before cancel.
    if trip.payment_method == "trip_day" and payment_service.trip_has_unpaid_bills(
        session, trip_id
    ):
        raise HTTPException(
            400,
            "Pay the outstanding daily bill before cancelling the remaining shifts.",
        )

    # ---- User refund maths ----
    # No refund is issued for a trip_day cancellation: the user paid only for
    # the days they actually used, and remaining days had no charge yet.
    # advance_20 / full_payment can only be cancelled pre-start, so any
    # upfront amount is fully refunded.
    user_refund = 0.0
    if (
        trip.payment_method in ("advance_20", "full_payment")
        and not has_completed_shift
    ):
        amt, refund_err = payment_service.calculate_refund_amount(session, trip_id)
        if refund_err:
            raise HTTPException(400, refund_err)
        user_refund = amt or 0.0
        if user_refund > 0:
            ok, err = payment_service.process_refund(
                session,
                trip_id,
                user_refund,
                reason="Trip cancelled by user",
            )
            if not ok:
                raise HTTPException(400, err or "Refund failed")

    # ---- Driver acceptance-fee refund ----
    # Refund only when no shift was ever started — once a shift completes, the
    # driver has earned the fee for that engagement.
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

    # ---- Void all pending future shifts ----
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

    if has_completed_shift:
        trip.status = "completed"
        if trip.actual_end_time is None:
            trip.actual_end_time = now_ist()
    else:
        trip.status = "cancelled_by_user"
    trip.state_version += 1
    session.add(trip)

    cancelled_driver_id = trip.driver_id

    # Tear down any pending offers.
    offers = session.exec(select(TripOffer).where(TripOffer.trip_id == trip.id)).all()
    for offer in offers:
        if offer.status == "pending":
            session.delete(offer)

    session.commit()

    settlement_id = None
    if has_completed_shift:
        billing_service = BillingService()
        ok, sid, _ = billing_service.generate_final_settlement(session, trip_id)
        if ok or sid:
            settlement_id = sid

    trip.driver_id = None
    trip.driver_accepted_at = None
    session.add(trip)
    session.commit()

    # Bust driver-availability cache so the freed driver can be re-ranked.
    if cancelled_driver_id and redis_client:
        try:
            redis_client.delete(f"driver_{cancelled_driver_id}")
        except Exception:
            pass

    # Notify the driver that the user cancelled (if a driver was assigned).
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
                    data={"type": "trip_cancelled", "trip_id": trip_id},
                )
        except Exception:
            pass

    return {
        "message": "Trip cancelled successfully",
        "refund_amount": user_refund,
        "driver_refund_amount": driver_refund,
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
        .where(TripOffer.driver_id == driver.id)
        .where(TripOffer.status == "pending")
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

    # Lock the offer row so a concurrent escalation pass can't delete it
    # mid-update.
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

    # Re-fetch the trip with a row lock before escalating so two parallel
    # rejects can't both trigger escalation for the same trip.
    trip = session.exec(
        select(Trip).where(Trip.id == offer.trip_id).with_for_update()
    ).first()
    if trip and trip.status == "searching":
        escalated = attempt_trip_escalation(session, trip)
        if escalated:
            session.commit()

    return {"message": "Offer rejected"}


@router.post("/check-escalation")
def check_and_escalate_tiers(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    count = process_tier_escalation(session)
    return {"message": f"Escalated {count} trips."}


@router.post("/{trip_id}/select-payment-method")
def select_payment_method(
    trip_id: int,
    payment_method: str = Body(..., embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Select or change the trip's payment method.

    Allowed moves: trip_day -> advance_20 / full_payment, and advance_20 ->
    full_payment. A trip can never move back to trip_day, and full_payment is
    final. advance_20 / full_payment require an upfront payment (see
    /pay-upfront) computed on the still-outstanding portion of the booking, so
    a mid-trip switch never re-charges shifts already completed and paid.
    """
    trip = session.exec(
        select(Trip).where(Trip.id == trip_id).with_for_update()
    ).first()
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(403, "Not authorized")

    if payment_method not in ("trip_day", "advance_20", "full_payment"):
        raise HTTPException(400, "Invalid payment method")

    # Outstation is billed once at the end of the trip with a flat 3% discount —
    # there is no payment-method choice for it.
    if (trip.hiring_type or "").strip().lower() == "outstation":
        raise HTTPException(
            400,
            "Outstation trips are billed once at trip end (3% discount) — "
            "the payment method cannot be selected or changed.",
        )

    current = trip.payment_method  # None before the driver accepts the booking

    # Transition matrix. An unset method may still be set to anything (initial
    # selection); otherwise only upgrades are allowed.
    if current is None:
        allowed = {"trip_day", "advance_20", "full_payment"}
    elif current == "trip_day":
        allowed = {"advance_20", "full_payment"}
    elif current == "advance_20":
        allowed = {"full_payment"}
    else:  # full_payment
        allowed = set()

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

    # Timing: never change while a shift is physically running, or once the
    # trip has closed.
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

    # Once every shift is done, only the final settlement is left — it must be
    # paid with the method already in effect.
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

    # Setting (or keeping) trip_day needs no upfront step.
    if payment_method == "trip_day":
        trip.payment_method = "trip_day"
        trip.state_version += 1
        session.add(trip)
        session.commit()
        return {"message": "Payment method set to trip_day"}

    # advance_20 / full_payment — compute the upfront due on the outstanding
    # portion only.
    portion = trip_service.compute_outstanding_portion(session, trip)
    outstanding_gross = portion["outstanding_gross"]
    if outstanding_gross <= 0:
        raise HTTPException(
            400, "Nothing left to bill — the payment method cannot be changed."
        )

    if payment_method == "advance_20":
        upfront = round(0.20 * outstanding_gross, 2)
        # Anti-fraud: the 20% upfront must at least cover the most recent
        # pending shift bill, so a switch can't be used to dodge an unpaid day.
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
    trip_id: int,
    payment_method: str = Body("card", embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """User upfront payment for an advance_20 / full_payment trip.

    The amount is computed on the still-outstanding portion of the booking
    (remaining shifts + any unpaid completed shift), so:
      * a fresh booking pays 20% / 95% of the whole fare, and
      * a mid-trip switch pays only for what is left, crediting whatever the
        user already paid.
    Once paid, any unpaid completed-shift bills are settled under the new
    method and the trip is un-blocked / un-paused so it can continue.
    trip_day has no upfront step (collected per day via /bill/{id}/pay).
    """
    trip = session.exec(
        select(Trip).where(Trip.id == trip_id).with_for_update()
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
        ok, err = payment_service.user_make_payment(
            session, trip_id, str(current_user.id), amount, payment_method
        )
        if not ok:
            raise HTTPException(400, err or "Upfront payment failed")

    # Settle any already-generated daily bills that still carry a balance,
    # under the new payment method. The credit pool (advance/full upfront
    # payments) is drawn down in shift-date order; trip_day cash that cleared
    # earlier days is excluded so the maths matches generate_daily_bill.
    discount_pct = payment_method_discount_pct(trip.hiring_type, trip.payment_method)
    per_day = portion["per_day"]

    total_upfront = sum(
        p.amount
        for p in session.exec(
            select(PaymentTransaction).where(
                PaymentTransaction.trip_id == trip_id,
                PaymentTransaction.payer_type == "user",
                PaymentTransaction.payment_status == "success",
                PaymentTransaction.payment_type.notin_(["trip_day_bill", "settlement"]),
            )
        ).all()
    )
    trip_day_cash = sum(
        p.amount
        for p in session.exec(
            select(PaymentTransaction).where(
                PaymentTransaction.trip_id == trip_id,
                PaymentTransaction.payer_type == "user",
                PaymentTransaction.payment_status == "success",
                PaymentTransaction.payment_type == "trip_day_bill",
            )
        ).all()
    )

    daily_bills = session.exec(
        select(TripBill)
        .where(
            TripBill.trip_id == trip_id,
            TripBill.bill_type == "daily_bill",
        )
        .order_by(TripBill.bill_date)
        .with_for_update()
    ).all()

    running_paid = 0.0
    now = now_ist()
    for bill in daily_bills:
        if (bill.amount_due or 0.0) > 0:
            disc = round(per_day * discount_pct / 100.0, 2)
            net = round(per_day - disc, 2)
            credit_available = max(
                0.0, total_upfront - max(0.0, running_paid - trip_day_cash)
            )
            paid = min(net, credit_available)
            bill.discount_percentage = discount_pct or None
            bill.discount_amount = disc
            bill.total_amount = net
            bill.amount_paid = paid
            bill.amount_due = max(0.0, round(net - paid, 2))
            bill.is_paid = bill.amount_due <= 0
            if bill.is_paid:
                bill.paid_at = now
                bill.paid_by = "user_online"
            if disc > 0 and isinstance(bill.components, list):
                bill.components = bill.components + [
                    {
                        "name": f"Payment Discount ({discount_pct:.0f}%)",
                        "amount": -disc,
                        "percentage": -discount_pct,
                    }
                ]
            session.add(bill)
        running_paid += bill.amount_paid or 0.0

    # Un-block and re-arm: a mid-trip switch is done from a `paused` (unpaid
    # bill) or `active_pending_otp` state — clear the block, flip any
    # payment-paused shift back to scheduled, and move a paused trip to
    # active_pending_otp so the next shift's OTP can be requested.
    trip.is_payment_blocked = False
    for att in session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status == "paused_payment",
        )
    ).all():
        att.status = "scheduled"
        att.skip_reason = None
        att.marked_by = "system"
        session.add(att)
    if trip.status == "paused":
        trip.status = (
            "active_pending_otp"
            if trip_service.has_pending_shifts(session, trip_id)
            else "completed"
        )
    trip.state_version += 1
    session.add(trip)
    session.commit()

    return {
        "message": "Upfront payment successful",
        "trip_id": trip_id,
        "amount_paid": amount,
        "payment_method": trip.payment_method,
        "trip_status": trip.status,
    }


@router.post("/driver/{trip_id}/accept-and-pay")
def driver_accept_and_initiate_payment(
    trip_id: int,
    action: str = Body(..., embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    # Row-lock the driver so two concurrent accepts by the same driver are
    # serialized — this is what makes the one-driver-one-trip check below race
    # free.
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id).with_for_update()
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can perform this action")

    trip = session.exec(
        select(Trip).where(Trip.id == trip_id).with_for_update()
    ).first()
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
        # Status guard under the row lock — another writer may have just
        # transitioned the trip out from under us before we got the lock.
        if trip.status not in ("searching", "accepted_pending_payment"):
            raise HTTPException(
                400,
                f"Trip cannot be accepted in status '{trip.status}'.",
            )

        # One driver = one active trip. Block accepting a new booking while the
        # driver still has another in-flight trip. The driver row is locked
        # above, so two concurrent accepts cannot both slip past this check.
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
                f"You already have an active trip (id={busy_trip.id}, "
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

        # Bust the driver-availability cache so this driver is no longer
        # eligible for ranking on other in-flight trips while they hold this one.
        redis_client.delete(f"driver_{driver.id}")

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
    payment_method: str = Body("card", embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    # payment_method accepted for forward-compat with future gateways; the
    # acceptance fee charge is server-controlled via driver_accept_payment.
    _ = payment_method
    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(403, "Only drivers can perform this action")

    # Row-lock the trip so this finalize-payment path can't interleave with
    # driver_payment_timeout_scheduler auto-rejecting the trip back to
    # `searching` between our status check and our state mutation.
    trip = session.exec(
        select(Trip).where(Trip.id == trip_id).with_for_update()
    ).first()
    if not trip:
        raise HTTPException(404, "Trip not found")

    if trip.driver_id != driver.id:
        raise HTTPException(403, "Not authorized")

    if trip.status != "accepted_pending_payment":
        raise HTTPException(
            400, f"Trip status is {trip.status}, cannot process payment now"
        )

    # Payment method is optional at booking time. Now that the driver has
    # accepted (by paying the acceptance fee), default it to trip-day billing
    # so the trip can proceed without waiting on the user — the user can still
    # switch to advance_20 / full_payment later via /select-payment-method.
    if not trip.payment_method:
        trip.payment_method = "trip_day"
        session.add(trip)

    payment_service = PaymentService(redis_client)
    success, error = payment_service.driver_accept_payment(session, trip_id, driver.id)

    if not success:
        raise HTTPException(400, f"Payment failed: {error}")

    trip_service = TripService()

    # Generate schedules upon successful driver payment
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

        is_outstation = (trip.hiring_type or "").strip().lower() == "outstation"
        end_date_for_attendance = trip.end_date or trip.start_date
        att_ok, att_err = trip_service.create_trip_attendance_records(
            session,
            trip_id,
            trip.start_date,
            end_date_for_attendance,
            effective_start_dt,
            duration_hours,
            selected_days=trip.selected_days,
            single_shift=is_outstation,
        )
        if not att_ok:
            raise HTTPException(400, att_err)

        if is_outstation:
            single_att = session.exec(
                select(TripAttendance).where(TripAttendance.trip_id == trip_id)
            ).first()
            if single_att:
                trip.scheduled_start_time = single_att.scheduled_start
                trip.scheduled_end_time = single_att.scheduled_end
                session.add(trip)

    success, error = trip_service.transition_trip_state(
        session, trip_id, "active_pending_otp", validate=True
    )

    if not success:
        raise HTTPException(400, f"Status update failed: {error}")

    redis_key = f"driver_payment_timer:{trip_id}:{driver.id}"
    redis_client.delete(redis_key)

    if trip.payment_method in ("advance_20", "full_payment"):
        return {
            "message": "Driver payment successful. Awaiting user upfront payment before OTP can be requested.",
            "trip_id": trip_id,
            "trip_status": trip.status,
            "next_step": "user_upfront_payment",
        }
    else:
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
        if trip.payment_method in ("advance_20", "full_payment"):
            raise HTTPException(
                400,
                f"Trip is paused — complete {trip.payment_method} upfront payment before requesting OTP.",
            )
        else:
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
    # Upper bound: don't hand out an OTP for a shift whose 12-hour window has
    # already lapsed. The expiry sweeper will mark it skipped_by_system shortly.
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
        session, trip_id, driver.id, otp, trip_date=shift_date
    )
    if not is_valid:
        raise HTTPException(400, error)

    trip_service = TripService()
    trip_service.transition_trip_state(session, trip_id, "active", validate=False)
    trip_service.transition_trip_state(session, trip_id, "ongoing", validate=False)

    verify_now = now_ist()
    # Set trip-level actual_start_time only the FIRST time a shift is verified.
    # Per-day starts are tracked on TripAttendance.actual_start; the trip-level
    # field should reflect when the booking actually began.
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
    body: TripDaySkipRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip_date = body.trip_date
    reason = body.reason

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

    # Outstation trips are a single continuous booking with one OTP and no
    # per-day shifts — there is nothing to skip. Use cancellation instead.
    if (trip.hiring_type or "").strip().lower() == "outstation":
        raise HTTPException(
            400,
            "Outstation trips cannot be skipped. Use the cancel option to drop this booking.",
        )

    today = today_ist()

    # Cannot skip a past day.
    if trip_date < today:
        raise HTTPException(400, "Cannot skip a past trip day.")

    # Skip is a same-day action. To drop a future day, use the cancellation
    # flow — preventing users/drivers from pre-emptively voiding shifts that
    # are still days away (e.g. tomorrow's shift right after today's ended).
    if trip_date != today:
        raise HTTPException(
            400,
            "Skip is only allowed for today's shift. To remove a future day, cancel the trip.",
        )

    # Driver-only restriction: once a shift is in progress (OTP verified, trip
    # ongoing) the correct action is end-trip, not skip.
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

    # If that was the last unfinished shift, generate the final settlement
    # immediately so the user has something to pay against without waiting
    # for the daily settlement scheduler.
    settlement_id = None
    pending = session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip_id,
            TripAttendance.status.in_(["scheduled", "paused_payment"]),
        )
    ).first()
    if not pending:
        billing_service = BillingService()
        ok, sid, _ = billing_service.generate_final_settlement(session, trip_id)
        if ok or sid:
            settlement_id = sid

    return {
        "message": f"Day {trip_date} marked as absent by {marked_by}",
        "trip_id": trip_id,
        "trip_date": trip_date.isoformat(),
        "settlement_id": settlement_id,
    }


@router.post("/{trip_id}/end-trip")
def driver_end_trip(
    trip_id: int,
    notes: Optional[str] = Body(None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    _ = notes  # currently unused; retained as accepted body field for the driver app
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

    end_now = now_ist()
    if not has_future_shifts:
        trip.actual_end_time = end_now
    else:
        trip.scheduled_start_time = next_attendance.scheduled_start
        trip.scheduled_end_time = next_attendance.scheduled_end

    session.add(trip)
    session.commit()

    # Stamp per-day actual_end on the closing attendance row so summaries
    # and analytics see when each shift actually ended.
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
                    "trip_id": trip_id,
                    "bill_id": bill_id,
                    "amount": amount,
                },
            )
        except Exception:
            pass

    # trip_day + future shifts: do NOT leave the trip in active_pending_otp
    # while today's daily bill is unpaid. Otherwise the user app loads
    # tomorrow's OTP screen and the bill-payment UI disappears. Hold the
    # trip in `paused` until /bill/{id}/pay clears the unpaid bill, at which
    # point unpause_trip_if_clear flips it back to active_pending_otp.
    if has_future_shifts and trip.payment_method == "trip_day":
        payment_service = PaymentService(redis_client)
        if payment_service.trip_has_unpaid_bills(session, trip_id):
            trip.status = "paused"
            trip.is_payment_blocked = True
            trip.state_version += 1
            session.add(trip)
            session.commit()

    # On the last shift, generate the final settlement inline so the user
    # has a payable record immediately (advance_20 / full_payment leftover
    # balance, or a zero-due close-out for trip_day). Without this the
    # frontend would have to wait for daily_settlement_scheduler to fire.
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
                            "trip_id": trip_id,
                            "settlement_id": sid,
                            "amount": settlement.remaining_due,
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
        "settlement_id": settlement_id,
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
        stmt = stmt.where(TripBill.amount_due > 0)
    return session.exec(stmt.order_by(TripBill.bill_date)).all()


@router.post("/bill/{bill_id}/pay")
def user_pay_bill(
    bill_id: int,
    payment_method: str = Body("card", embed=True),
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
        session, bill_id, current_user.id, payment_method
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
    note: Optional[str] = Body(None, embed=True),
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
    payment_method: str = Body("card", embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """User pays the final trip settlement for advance_20 or full_payment methods."""
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

    payment_service = PaymentService(redis_client)
    success, txn_id = payment_service.process_dummy_payment(
        amount=settlement.remaining_due,
        payer_id=str(current_user.id),
        payer_type="user",
        payment_method=payment_method,
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
        payment_method=payment_method,
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
            TripBill.trip_id == settlement.trip_id,
            TripBill.is_paid == False,  # noqa: E712 (SQL boolean cmp)
        )
    ).all()
    paid_at_now = now_ist()
    for bill in unpaid_bills:
        bill.is_paid = True
        bill.amount_paid = bill.total_amount
        bill.amount_due = 0.0
        bill.paid_at = paid_at_now
        bill.paid_by = "user_online"
        session.add(bill)

    # Lift any payment-block state that was tied to the cleared bills, so a
    # paused trip can resume. Without this, a paused trip remains paused
    # even after the user pays the final settlement.
    payment_service.unpause_trip_if_clear(session, settlement.trip_id)

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
