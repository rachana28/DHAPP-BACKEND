from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlmodel import Session, select, desc
from typing import Any, Dict, List
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import NoResultFound
from pydantic import BaseModel
import redis

from app.core import cache
from app.core.database import get_session, get_redis
from app.core.models import (
    TowTrip,
    TowTripCreate,
    TowTripSafe,
    TowTripReadUser,
    TowTruckDriver,
    TowTruckDriverReview,
    TowTripOffer,
    TowTripOfferPublic,
    BookingAddressUpdate,
    User,
)
from app.modules.bookings import otp_service as booking_otp_service
from app.modules.bookings.summary_helpers import address_edit_window
from app.modules.bookings.ratings import (
    RatingIn,
    record_service_rating,
    recompute_provider_average,
    ensure_rateable,
)
from app.modules.tow_transport.arrival_service import mark_tow_arrived
from app.modules.tow_transport.booking_summary import build_tow_summary
from app.modules.pricing.pricing_algo import (
    get_road_distance_duration,
    _calculate_service_cost,
    TRANSPORT_VEHICLE_TYPES,
)
from app.workers.topics import telemetry_topic
from app.utils.time_utils import now_ist
from app.modules.payments.service import refund_booking_payments
from app.services.dues import raise_if_unpaid_past_due
from app.core.security import get_current_user, get_current_active_tow_truck_driver
from app.modules.tow_transport.tow_allocation import (
    rank_tow_drivers,
    create_tow_offers_for_tier,
    attempt_tow_trip_escalation,
)
from app.modules.dispatch import geo
from app.utils.notifications import send_push_notification
from app.utils.id_generator import generate_reference_id, get_by_reference, TOW_TRIP

router = APIRouter(prefix="/tow-transport-trips", tags=["Tow & Transport Trips"])


def _price_tow_trip(session, redis_client, trip: TowTrip) -> None:
    """Authoritatively (re)set distance_km + fare + fare_breakdown on a tow trip
    from its tow-truck class and route. Server-side so a quote and the charge
    can't diverge, and so an address edit re-prices consistently."""
    if (
        trip.start_lat is not None
        and trip.start_lng is not None
        and trip.end_lat is not None
        and trip.end_lng is not None
    ):
        dist, _ = get_road_distance_duration(
            trip.start_lat, trip.start_lng, trip.end_lat, trip.end_lng
        )
        if dist:
            trip.distance_km = round(dist, 2)
    # Service-aware pricing: tow → pricing_tow_*, transport → pricing_transport_*.
    # For tow trips (service_type defaults to "tow") this is identical to before.
    price = _calculate_service_cost(
        trip.service_type or "tow",
        trip.distance_km or 0.0,
        trip.requested_vehicle_class,
        session,
        redis_client,
    )
    trip.fare = price["final_price"]
    trip.fare_breakdown = price["breakdown"]


@router.post("/book-request", response_model=TowTripSafe)
def create_tow_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    trip_in: TowTripCreate,
):
    raise_if_unpaid_past_due(session, current_user.id)

    trip_data = trip_in.model_dump(exclude_unset=True)
    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"
    trip_data.pop("hiring_type", None)  # discriminator no longer stored

    service_type = (trip_data.get("service_type") or "tow").strip().lower()
    trip_data["service_type"] = service_type
    # Transport is a new service: require a valid transport class up front. The tow
    # path keeps its existing lenient behaviour (normalized at pricing time).
    if service_type == "transport":
        chosen = (trip_data.get("transport_vehicle_type") or "").strip().lower()
        if chosen not in TRANSPORT_VEHICLE_TYPES:
            raise HTTPException(
                400,
                "transport_vehicle_type must be one of: "
                + ", ".join(TRANSPORT_VEHICLE_TYPES),
            )
        trip_data["transport_vehicle_type"] = chosen

    db_trip = TowTrip.model_validate(trip_data)
    db_trip.reference_id = generate_reference_id(session, TOW_TRIP)
    # Server-authoritative pricing keyed on the requested provider class.
    _price_tow_trip(session, redis_client, db_trip)

    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    # Strict dispatch: only providers of this service + requested class.
    ranked_drivers = rank_tow_drivers(
        session,
        db_trip.start_lat,
        db_trip.start_lng,
        service_type=db_trip.service_type or "tow",
        vehicle_class=db_trip.requested_vehicle_class,
    )

    if not ranked_drivers:
        db_trip.status = "no_drivers_found"
        session.add(db_trip)
        session.commit()
        return db_trip

    tier_size = geo.get_config_int(session, geo.KNN_LIMIT_KEY, geo.DEFAULT_KNN_LIMIT)
    tier_1_drivers = ranked_drivers[:tier_size]
    create_tow_offers_for_tier(session, db_trip.id, tier_1_drivers, tier=1)

    return db_trip


@router.get("/my-bookings", response_model=List[TowTripReadUser])
def get_my_tow_bookings(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    if current_user.role == "tow_truck_driver":
        driver = session.exec(
            select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
        ).first()
        if not driver:
            return []

        statement = (
            select(TowTrip)
            .where(TowTrip.tow_truck_driver_id == driver.id)
            .order_by(desc(TowTrip.booking_time))
            .offset(offset)
            .limit(limit)
            .options(selectinload(TowTrip.user))
        )
        return session.exec(statement).all()

    elif current_user.role == "user":
        statement = (
            select(TowTrip)
            .where(TowTrip.user_id == current_user.id)
            .order_by(desc(TowTrip.booking_time))
            .offset(offset)
            .limit(limit)
            .options(selectinload(TowTrip.tow_truck_driver))
        )
        return session.exec(statement).all()

    else:
        return []


@router.get("/driver/active-bookings", response_model=List[TowTripReadUser])
def get_tow_driver_active_bookings(
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    """Driver-app polling target: the tow driver's currently-engaged jobs only
    (accepted → near_destination). Short-TTL cached; carries no customer
    phone/address (TowTripReadUser exposes no `user` block)."""
    key = cache.active_key("tow_driver", current_driver.id)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached

    rows = session.exec(
        select(TowTrip)
        .where(
            TowTrip.tow_truck_driver_id == current_driver.id,
            TowTrip.status.in_(geo.TOW_ACTIVE_STATES),
        )
        .order_by(desc(TowTrip.booking_time))
        .options(selectinload(TowTrip.tow_truck_driver))
    ).all()
    result = [
        TowTripReadUser.model_validate(t, from_attributes=True).model_dump(mode="json")
        for t in rows
    ]
    cache.cache_set_json(key, result, cache.ACTIVE_CACHE_TTL)
    return result


@router.post("/{trip_id}/cancel")
def cancel_tow_trip(
    trip_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Cancels a Tow Trip and removes all associated offers.
    """
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail="Not authorized to cancel this trip"
        )

    if trip.status in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel a completed or already cancelled trip",
        )

    # Free cancellation only pre-pickup. Once the tow is physically underway
    # (in_progress / near_destination) it can't be cancelled from the app.
    if trip.status in ("in_progress", "near_destination"):
        raise HTTPException(
            status_code=400,
            detail="The tow is already underway and can no longer be cancelled.",
        )

    driver_user_id_to_notify = None
    if trip.tow_truck_driver_id:
        # If a driver was already assigned, we must tell them it's cancelled
        driver = session.get(TowTruckDriver, trip.tow_truck_driver_id)
        if driver:
            driver_user_id_to_notify = driver.user_id

    # Update Status
    trip.status = "cancelled"
    session.add(trip)

    # Delete All Offers (Pending or Accepted)
    offers = session.exec(
        select(TowTripOffer).where(TowTripOffer.trip_id == trip.id)
    ).all()
    for offer in offers:
        session.delete(offer)

    session.commit()

    refund_booking_payments(
        session,
        "tow",
        trip.reference_id,
        reason="Booking cancelled by user",
        actor="user",
        actor_id=str(current_user.id),
    )

    if driver_user_id_to_notify:
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[driver_user_id_to_notify],
            title="Trip Cancelled ❌",
            body="The customer has cancelled this request.",
            data={"trip_id": trip.reference_id, "type": "cancellation"},
        )

    return {"message": "Tow trip cancelled successfully"}


@router.get("/driver/offers", response_model=List[TowTripOfferPublic])
def get_tow_driver_offers(
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    # Only actionable offers: pending AND whose trip is still searching (drop
    # offers for trips already taken/cancelled). Capped for a lean payload.
    statement = (
        select(TowTripOffer)
        .join(TowTrip, TowTripOffer.trip_id == TowTrip.id)
        .where(TowTripOffer.tow_truck_driver_id == current_driver.id)
        .where(TowTripOffer.status == "pending")
        .where(TowTrip.status == "searching")
        .order_by(desc(TowTripOffer.created_at))
        .limit(20)
        .options(selectinload(TowTripOffer.trip))
    )
    offers = session.exec(statement).all()
    return offers


@router.post("/driver/accept-offer/{offer_id}")
def accept_tow_offer(
    offer_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    offer = session.get(TowTripOffer, offer_id)
    if not offer or offer.tow_truck_driver_id != current_driver.id:
        raise HTTPException(404, "Offer not found")

    if offer.status != "pending":
        raise HTTPException(400, "Offer not valid")

    # CRITICAL: Lock the Trip Row
    try:
        statement = select(TowTrip).where(TowTrip.id == offer.trip_id).with_for_update()
        trip = session.exec(statement).one()
    except NoResultFound:
        raise HTTPException(404, "Trip not found")

    # Safe Status Check (Guaranteed by Lock)
    if trip.status != "searching":
        session.rollback()  # Release lock immediately
        raise HTTPException(400, "Trip already taken by another driver")

    trip.tow_truck_driver_id = current_driver.id
    trip.status = "accepted"
    offer.status = "accepted"

    session.add(trip)
    session.add(offer)

    others = session.exec(
        select(TowTripOffer).where(TowTripOffer.trip_id == trip.id)
    ).all()
    for o in others:
        if o.id != offer.id:
            session.delete(o)

    session.commit()

    try:
        # EXECUTE IN BACKGROUND (Non-blocking)
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[trip.user_id],  # Pass as list
            title="Tow Truck Confirmed! 🚛",
            body=f"{current_driver.name} is on the way.",
            data={"trip_id": trip.reference_id, "screen": "tracking"},
        )
    except Exception as e:
        print(f"Notification error: {e}")

    return {
        "message": "Trip accepted",
        "trip_id": trip.reference_id,
        # Per-ride MQTT topic: driver app publishes GPS here; user app subscribes.
        "telemetry_topic": telemetry_topic(trip.reference_id),
    }


@router.post("/driver/reject-offer/{offer_id}")
def reject_tow_offer(
    offer_id: int,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    offer = session.get(TowTripOffer, offer_id)
    if not offer or offer.tow_truck_driver_id != current_driver.id:
        raise HTTPException(404, "Offer not found")

    offer.status = "rejected"
    session.add(offer)
    session.commit()

    trip = session.get(TowTrip, offer.trip_id)
    if trip and trip.status == "searching":
        escalated = attempt_tow_trip_escalation(session, trip)
        if escalated:
            session.commit()

    return {"message": "Offer rejected"}


class OTPVerifyIn(BaseModel):
    otp: str


@router.post("/{trip_id}/verify-otp")
def verify_tow_otp(
    trip_id: str,
    body: OTPVerifyIn,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    """Tow driver manually enters the OTP at the pickup → the tow STARTS."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.tow_truck_driver_id != current_driver.id:
        raise HTTPException(404, "Trip not found")

    if trip.status != "arrived":
        raise HTTPException(
            400,
            "OTP can only be verified once you've reached the pickup point.",
        )

    ok, err = booking_otp_service.verify(
        session, "tow", trip.id, body.otp, current_driver.user_id
    )
    if not ok:
        raise HTTPException(400, err)

    trip.status = "in_progress"
    trip.actual_start_time = now_ist()
    session.add(trip)
    session.commit()

    background_tasks.add_task(
        send_push_notification,
        session=session,
        user_ids=[trip.user_id],
        title="Tow Started 🚛",
        body="Your vehicle is now being towed.",
        data={"trip_id": trip.reference_id, "screen": "tracking"},
    )
    return {"message": "OTP verified. Tow started.", "status": trip.status}


@router.post("/{trip_id}/regenerate-otp")
def regenerate_tow_otp(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """User regenerates the start-OTP after the previous one expired."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(404, "Trip not found")

    if trip.status != "arrived":
        raise HTTPException(
            400, "An OTP is only available once the driver has reached the pickup."
        )

    code = booking_otp_service.generate(session, "tow", trip.id)
    return {"otp": code, "message": "Share this OTP with the tow driver."}


@router.post("/{trip_id}/end-trip")
def end_tow_trip(
    trip_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    """Driver ends the tow at the drop-off → COMPLETED; payment then proceeds."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.tow_truck_driver_id != current_driver.id:
        raise HTTPException(404, "Trip not found")

    if trip.status not in ("in_progress", "near_destination"):
        raise HTTPException(400, "Trip is not in progress.")

    trip.status = "completed"
    trip.actual_end_time = now_ist()
    if trip.payment_due_at is None:
        trip.payment_due_at = now_ist()
    session.add(trip)
    session.commit()

    background_tasks.add_task(
        send_push_notification,
        session=session,
        user_ids=[trip.user_id],
        title="Tow Completed ✅",
        body="Your vehicle has reached the destination. Please complete the payment.",
        data={"trip_id": trip.reference_id, "screen": "payment"},
    )
    return {"message": "Tow completed.", "status": trip.status}


@router.post("/{trip_id}/mark-arrived")
def mark_tow_arrived_endpoint(
    trip_id: str,
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    """Driver manually marks arrival at the pickup — a fallback for when the
    telemetry geofence doesn't fire (no broker / GPS drift). Generates and
    pushes the start OTP, mirroring the automatic geofence trigger."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.tow_truck_driver_id != current_driver.id:
        raise HTTPException(404, "Trip not found")

    if trip.status == "arrived":
        return {"message": "Already marked as arrived.", "status": trip.status}
    if trip.status != "accepted":
        raise HTTPException(
            400,
            "Arrival can only be marked after accepting and before the tow starts.",
        )

    mark_tow_arrived(session, trip)
    return {
        "message": "Marked as arrived. The customer has been sent the OTP.",
        "status": trip.status,
    }


@router.get("/{trip_id}/summary")
def get_tow_trip_summary(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Rich status/tracking summary for the user app's Tow screen (parity with
    the regular-trip summary). Visible to the booking's owner or its driver."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    is_owner = trip.user_id == current_user.id
    is_provider = False
    if not is_owner and current_user.role == "tow_truck_driver":
        driver = session.exec(
            select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
        ).first()
        is_provider = bool(driver and trip.tow_truck_driver_id == driver.id)
    if not (is_owner or is_provider):
        raise HTTPException(403, "Not authorized to view this trip")

    return build_tow_summary(
        session, trip, viewer="provider" if is_provider else "user"
    )


# --- REVIEWS (booking-scoped; only after the tow/transport job is completed) ---


@router.post("/{trip_id}/service-review")
def submit_tow_service_review(
    trip_id: str,
    payload: RatingIn,
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Rate DriveHub's service for a completed tow/transport booking (the app
    rating, not the driver). Recorded under the booking's own ``service_type``
    so admin can tell tow apart from transport."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trip not found")
    service_type = trip.service_type or "tow"
    ensure_rateable(service_type, trip.status)
    return record_service_rating(
        session,
        service_type=service_type,
        booking_id=trip.id,
        booking_reference_id=trip.reference_id,
        user_id=current_user.id,
        payload=payload,
    )


@router.post("/{trip_id}/provider-review")
def submit_tow_provider_review(
    trip_id: str,
    payload: RatingIn,
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Rate the tow/transport driver who handled a completed booking."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trip not found")
    ensure_rateable(trip.service_type or "tow", trip.status)
    if not trip.tow_truck_driver_id:
        raise HTTPException(status_code=400, detail="No driver handled this trip")

    existing = session.exec(
        select(TowTruckDriverReview).where(
            TowTruckDriverReview.user_id == current_user.id,
            TowTruckDriverReview.trip_id == trip.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="You have already reviewed this trip")

    session.add(
        TowTruckDriverReview(
            driver_id=trip.tow_truck_driver_id,
            trip_id=trip.id,
            user_id=current_user.id,
            rating=payload.rating,
            comment=payload.comment,
        )
    )
    driver = session.get(TowTruckDriver, trip.tow_truck_driver_id)
    recompute_provider_average(
        session, TowTruckDriverReview, "driver_id", driver.id, driver
    )
    session.commit()
    cache.cache_delete(cache.me_key("tow_driver", driver.user_id))
    return {"message": "Review submitted successfully", "rating": payload.rating}


@router.patch("/{trip_id}/address")
def update_tow_trip_address(
    trip_id: str,
    body: BookingAddressUpdate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
) -> Dict[str, Any]:
    """Edit pickup/destination within the 3-minute post-booking window. Re-prices
    the trip; re-dispatches if still searching and the pickup moved; otherwise
    notifies the already-assigned driver. Returns the refreshed summary."""
    trip = get_by_reference(session, TowTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(404, "Trip not found")

    editable, _ = address_edit_window(session, trip)
    if not editable:
        raise HTTPException(
            400,
            "Address can no longer be edited (the 3-minute window has passed or the tow has started).",
        )

    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "No address fields provided.")
    pickup_moved = any(k in data for k in ("start_lat", "start_lng", "start_location"))
    for key, value in data.items():
        setattr(trip, key, value)

    # Re-derive distance + fare from the new route (server-authoritative).
    _price_tow_trip(session, redis_client, trip)
    session.add(trip)
    session.commit()
    session.refresh(trip)

    if trip.status == "searching" and pickup_moved:
        # Re-dispatch from the new pickup: clear pending offers, re-rank.
        for offer in session.exec(
            select(TowTripOffer).where(TowTripOffer.trip_id == trip.id)
        ).all():
            session.delete(offer)
        session.commit()
        ranked = rank_tow_drivers(
            session,
            trip.start_lat,
            trip.start_lng,
            service_type=trip.service_type or "tow",
            vehicle_class=trip.requested_vehicle_class,
        )
        if ranked:
            tier_size = geo.get_config_int(
                session, geo.KNN_LIMIT_KEY, geo.DEFAULT_KNN_LIMIT
            )
            create_tow_offers_for_tier(session, trip.id, ranked[:tier_size], tier=1)
        else:
            trip.status = "no_drivers_found"
            session.add(trip)
            session.commit()
    elif trip.status == "accepted" and trip.tow_truck_driver_id:
        # Keep the assigned driver; just notify them of the change.
        driver = session.get(TowTruckDriver, trip.tow_truck_driver_id)
        if driver:
            background_tasks.add_task(
                send_push_notification,
                session=session,
                user_ids=[driver.user_id],
                title="Trip Updated 📍",
                body="The customer updated the pickup/destination. Please review the new route.",
                data={
                    "trip_id": trip.reference_id,
                    "screen": "tracking",
                    "type": "address_update",
                },
            )

    return build_tow_summary(session, trip)
