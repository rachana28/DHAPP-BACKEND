from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select, desc
from typing import Any, Dict, List
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import NoResultFound
from pydantic import BaseModel

from app.core.database import get_session
from app.core.models import (
    MechanicTrip,
    MechanicTripCreate,
    MechanicTripSafe,
    MechanicTripReadUser,
    Mechanic,
    MechanicOffer,
    MechanicOfferPublic,
    BookingAddressUpdate,
    User,
)
from app.core.security import get_current_user, get_current_active_mechanic
from app.modules.mechanic.mechanic_allocation import (
    rank_mechanics,
    create_mechanic_offers_for_tier,
    attempt_mechanic_trip_escalation,
)
from app.modules.dispatch import geo
from app.utils.notifications import send_push_notification
from app.utils.id_generator import (
    generate_reference_id,
    get_by_reference,
    MECHANIC_TRIP,
)
from app.modules.payments.service import refund_booking_payments
from app.services.dues import raise_if_unpaid_past_due
from app.modules.trips import booking_otp_service
from app.modules.trips.arrival_service import mark_mechanic_arrived
from app.modules.trips.booking_summary import (
    build_mechanic_summary,
    address_edit_window,
)
from app.workers.topics import telemetry_topic
from app.utils.time_utils import now_ist

router = APIRouter(prefix="/mechanic-trips", tags=["Mechanic Trips"])


class StatusUpdate(BaseModel):
    status: str  # "available" or "offline"


@router.post("/book-request", response_model=MechanicTripSafe)
def create_mechanic_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_in: MechanicTripCreate,
):
    raise_if_unpaid_past_due(session, current_user.id)

    trip_data = trip_in.model_dump(exclude_unset=True)
    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"
    trip_data.pop("hiring_type", None)  # discriminator no longer stored

    db_trip = MechanicTrip.model_validate(trip_data)
    db_trip.reference_id = generate_reference_id(session, MECHANIC_TRIP)
    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    ranked_mechanics = rank_mechanics(session, db_trip.start_lat, db_trip.start_lng)

    if not ranked_mechanics:
        db_trip.status = "no_mechanics_found"
        session.add(db_trip)
        session.commit()
        return db_trip

    tier_size = geo.get_config_int(session, geo.KNN_LIMIT_KEY, geo.DEFAULT_KNN_LIMIT)
    tier_1_mechanics = ranked_mechanics[:tier_size]
    create_mechanic_offers_for_tier(session, db_trip.id, tier_1_mechanics, tier=1)

    return db_trip


@router.get("/my-bookings", response_model=List[MechanicTripReadUser])
def get_my_mechanic_bookings(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Fetch mechanic bookings for the logged-in user or mechanic.
    """
    if current_user.role == "mechanic":
        # If the logged-in user is a mechanic, fetch trips assigned to them
        mechanic = session.exec(
            select(Mechanic).where(Mechanic.user_id == current_user.id)
        ).first()

        if not mechanic:
            return []

        statement = (
            select(MechanicTrip)
            .where(MechanicTrip.mechanic_id == mechanic.id)
            .order_by(desc(MechanicTrip.booking_time))
            .options(selectinload(MechanicTrip.user))
        )
        return session.exec(statement).all()

    elif current_user.role == "user":
        # If the logged-in user is a customer, fetch their mechanic requests
        statement = (
            select(MechanicTrip)
            .where(MechanicTrip.user_id == current_user.id)
            .order_by(desc(MechanicTrip.booking_time))
            .options(selectinload(MechanicTrip.mechanic))
        )
        return session.exec(statement).all()

    else:
        return []


@router.post("/{trip_id}/cancel")
def cancel_mechanic_trip(
    trip_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.status in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=400, detail="Cannot cancel a completed or cancelled trip"
        )

    # Free cancellation only pre-pickup; once the mechanic is actively servicing
    # (in_progress) it can no longer be cancelled from the app.
    if trip.status == "in_progress":
        raise HTTPException(
            status_code=400,
            detail="The service is already underway and can no longer be cancelled.",
        )

    mechanic_user_id_to_notify = None
    if trip.mechanic_id:
        mechanic = session.get(Mechanic, trip.mechanic_id)
        if mechanic:
            mechanic_user_id_to_notify = mechanic.user_id

    trip.status = "cancelled"
    session.add(trip)

    offers = session.exec(
        select(MechanicOffer).where(MechanicOffer.trip_id == trip.id)
    ).all()
    for offer in offers:
        session.delete(offer)

    session.commit()

    refund_booking_payments(
        session,
        "mechanic",
        trip.reference_id,
        reason="Booking cancelled by user",
        actor="user",
        actor_id=str(current_user.id),
    )

    if mechanic_user_id_to_notify:
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[mechanic_user_id_to_notify],
            title="Service Cancelled ❌",
            body="The customer has cancelled this mechanic request.",
            data={"trip_id": trip.reference_id, "type": "cancellation"},
        )

    return {"message": "Mechanic trip cancelled successfully"}


@router.get("/mechanic/offers", response_model=List[MechanicOfferPublic])
def get_mechanic_offers(
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    """Fetch pending offers for the logged-in mechanic."""
    statement = (
        select(MechanicOffer)
        .where(MechanicOffer.mechanic_id == current_mechanic.id)
        .where(MechanicOffer.status == "pending")
        .options(selectinload(MechanicOffer.trip))  # Loads the nested trip data from DB
    )
    offers = session.exec(statement).all()
    return offers


@router.post("/mechanic/accept-offer/{offer_id}")
def accept_mechanic_offer(
    offer_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    offer = session.get(MechanicOffer, offer_id)
    if not offer or offer.mechanic_id != current_mechanic.id:
        raise HTTPException(404, "Offer not found")

    if offer.status != "pending":
        raise HTTPException(400, "Offer not valid")

    try:
        # Lock the Trip Row to prevent race conditions
        statement = (
            select(MechanicTrip)
            .where(MechanicTrip.id == offer.trip_id)
            .with_for_update()
        )
        trip = session.exec(statement).one()
    except NoResultFound:
        raise HTTPException(404, "Trip not found")

    if trip.status != "searching":
        session.rollback()
        raise HTTPException(400, "Request already accepted by another mechanic")

    trip.mechanic_id = current_mechanic.id
    trip.status = "accepted"
    offer.status = "accepted"

    session.add(trip)
    session.add(offer)

    # Delete pending offers for other mechanics for this trip
    others = session.exec(
        select(MechanicOffer).where(MechanicOffer.trip_id == trip.id)
    ).all()
    for o in others:
        if o.id != offer.id:
            session.delete(o)

    session.commit()

    background_tasks.add_task(
        send_push_notification,
        session=session,
        user_ids=[trip.user_id],
        title="Mechanic Confirmed! 🛠️",
        body=f"{current_mechanic.name} is on the way.",
        data={"trip_id": trip.reference_id, "screen": "tracking"},
    )

    return {
        "message": "Service accepted",
        "trip_id": trip.reference_id,
        # Per-ride MQTT topic: mechanic app publishes GPS here; user app subscribes.
        "telemetry_topic": telemetry_topic(trip.reference_id),
    }


@router.post("/mechanic/reject-offer/{offer_id}")
def reject_mechanic_offer(
    offer_id: int,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    offer = session.get(MechanicOffer, offer_id)
    if not offer or offer.mechanic_id != current_mechanic.id:
        raise HTTPException(404, "Offer not found")

    # Mark the mechanic's offer as rejected
    offer.status = "rejected"
    session.add(offer)
    session.commit()

    # Immediately check if we need to escalate to the next tier
    trip = session.get(MechanicTrip, offer.trip_id)
    if trip and trip.status == "searching":
        escalated = attempt_mechanic_trip_escalation(session, trip)
        if escalated:
            session.commit()

    return {"message": "Offer rejected"}


@router.patch("/mechanic/status")
def update_mechanic_status(
    status_data: StatusUpdate,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    if status_data.status not in ["available", "offline"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    current_mechanic.status = status_data.status
    session.add(current_mechanic)
    session.commit()

    return {"message": f"Status updated to {status_data.status}"}


class OTPVerifyIn(BaseModel):
    otp: str


@router.post("/{trip_id}/verify-otp")
def verify_mechanic_otp(
    trip_id: str,
    body: OTPVerifyIn,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    """Mechanic enters the on-site OTP → the service STARTS (in_progress).

    The job is finished separately via ``POST /{trip_id}/complete`` once the
    repair is actually done, so 'completed' reflects work done — not arrival."""
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip or trip.mechanic_id != current_mechanic.id:
        raise HTTPException(404, "Trip not found")

    if trip.status != "arrived":
        raise HTTPException(
            400, "OTP can only be verified once you've reached the customer."
        )

    ok, err = booking_otp_service.verify(
        session, "mechanic", trip.id, body.otp, current_mechanic.user_id
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
        title="Service Started 🛠️",
        body="Your mechanic has started working on your vehicle.",
        data={"trip_id": trip.reference_id, "screen": "tracking"},
    )
    return {"message": "OTP verified. Service started.", "status": trip.status}


@router.post("/{trip_id}/complete")
def complete_mechanic_trip(
    trip_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    """Mechanic finishes the repair → COMPLETED; the user is then prompted to pay."""
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip or trip.mechanic_id != current_mechanic.id:
        raise HTTPException(404, "Trip not found")

    if trip.status != "in_progress":
        raise HTTPException(
            400, "The service must be started (OTP verified) before completing it."
        )

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
        title="Service Completed ✅",
        body="Your mechanic service is complete. Please complete the payment.",
        data={"trip_id": trip.reference_id, "screen": "payment"},
    )
    return {"message": "Service completed.", "status": trip.status}


@router.post("/{trip_id}/regenerate-otp")
def regenerate_mechanic_otp(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """User regenerates the on-site service-start OTP after the previous one expired."""
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(404, "Trip not found")

    if trip.status != "arrived":
        raise HTTPException(
            400, "An OTP is only available once the mechanic has reached you."
        )

    code = booking_otp_service.generate(session, "mechanic", trip.id)
    return {"otp": code, "message": "Share this OTP with the mechanic."}


@router.post("/{trip_id}/mark-arrived")
def mark_mechanic_arrived_endpoint(
    trip_id: str,
    session: Session = Depends(get_session),
    current_mechanic: Mechanic = Depends(get_current_active_mechanic),
):
    """Mechanic manually marks arrival at the customer — a fallback for when the
    telemetry geofence doesn't fire. Generates and pushes the on-site OTP."""
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip or trip.mechanic_id != current_mechanic.id:
        raise HTTPException(404, "Trip not found")

    if trip.status == "arrived":
        return {"message": "Already marked as arrived.", "status": trip.status}
    if trip.status != "accepted":
        raise HTTPException(
            400,
            "Arrival can only be marked after accepting and before starting the service.",
        )

    mark_mechanic_arrived(session, trip)
    return {
        "message": "Marked as arrived. The customer has been sent the OTP.",
        "status": trip.status,
    }


@router.get("/{trip_id}/summary")
def get_mechanic_trip_summary(
    trip_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Rich status/tracking summary for the user app's Mechanic screen. Visible
    to the booking's owner or its assigned mechanic."""
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")

    authorized = trip.user_id == current_user.id
    if not authorized and current_user.role == "mechanic":
        mechanic = session.exec(
            select(Mechanic).where(Mechanic.user_id == current_user.id)
        ).first()
        authorized = bool(mechanic and trip.mechanic_id == mechanic.id)
    if not authorized:
        raise HTTPException(403, "Not authorized to view this trip")

    return build_mechanic_summary(session, trip)


@router.patch("/{trip_id}/address")
def update_mechanic_trip_address(
    trip_id: str,
    body: BookingAddressUpdate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Edit the service location within the 3-minute post-booking window.
    (Mechanic visit fee is location-independent, so only the start_* fields
    apply and the fare is unchanged.) Re-dispatches if still searching and the
    location moved; otherwise notifies the assigned mechanic."""
    trip = get_by_reference(session, MechanicTrip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(404, "Trip not found")

    editable, _ = address_edit_window(session, trip)
    if not editable:
        raise HTTPException(
            400,
            "Address can no longer be edited (the 3-minute window has passed or the service has started).",
        )

    data = body.model_dump(exclude_unset=True)
    # Mechanic comes to the user, so only the start (service) location applies.
    start_fields = {
        k: v
        for k, v in data.items()
        if k in ("start_location", "start_lat", "start_lng")
    }
    if not start_fields:
        raise HTTPException(400, "No service-location fields provided.")
    location_moved = any(k in start_fields for k in ("start_lat", "start_lng"))
    for key, value in start_fields.items():
        setattr(trip, key, value)
    session.add(trip)
    session.commit()
    session.refresh(trip)

    if trip.status == "searching" and location_moved:
        for offer in session.exec(
            select(MechanicOffer).where(MechanicOffer.trip_id == trip.id)
        ).all():
            session.delete(offer)
        session.commit()
        ranked = rank_mechanics(session, trip.start_lat, trip.start_lng)
        if ranked:
            tier_size = geo.get_config_int(
                session, geo.KNN_LIMIT_KEY, geo.DEFAULT_KNN_LIMIT
            )
            create_mechanic_offers_for_tier(
                session, trip.id, ranked[:tier_size], tier=1
            )
        else:
            trip.status = "no_mechanics_found"
            session.add(trip)
            session.commit()
    elif trip.status == "accepted" and trip.mechanic_id:
        mechanic = session.get(Mechanic, trip.mechanic_id)
        if mechanic:
            background_tasks.add_task(
                send_push_notification,
                session=session,
                user_ids=[mechanic.user_id],
                title="Service Location Updated 📍",
                body="The customer updated the service location. Please review.",
                data={
                    "trip_id": trip.reference_id,
                    "screen": "tracking",
                    "type": "address_update",
                },
            )

    return build_mechanic_summary(session, trip)
