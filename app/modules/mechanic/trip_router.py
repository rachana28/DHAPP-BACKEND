from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select, desc
from typing import List
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import NoResultFound
from pydantic import BaseModel

from app.core.database import get_session
from app.core.models import (
    Trip,
    TripCreate,
    TripSafe,
    Mechanic,
    MechanicOffer,
    TripOfferPublic,
    User,
    TripReadUser,
)
from app.core.security import get_current_user, get_current_active_mechanic
from app.modules.mechanic.mechanic_allocation import (
    rank_mechanics,
    create_mechanic_offers_for_tier,
    attempt_mechanic_trip_escalation,
)
from app.utils.notifications import send_push_notification

router = APIRouter(prefix="/mechanic-trips", tags=["Mechanic Trips"])

class StatusUpdate(BaseModel):
    status: str  # "available" or "offline"


@router.post("/book-request", response_model=TripSafe)
def create_mechanic_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_in: TripCreate,
):
    trip_data = trip_in.model_dump()
    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"
    trip_data["hiring_type"] = "Mechanic Service"

    db_trip = Trip.model_validate(trip_data)
    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    ranked_mechanics = rank_mechanics(session)

    if not ranked_mechanics:
        db_trip.status = "no_mechanics_found"
        session.add(db_trip)
        session.commit()
        return db_trip

    tier_1_mechanics = ranked_mechanics[:3]
    create_mechanic_offers_for_tier(session, db_trip.id, tier_1_mechanics, tier=1)

    return db_trip


@router.get("/my-bookings", response_model=List[TripReadUser])
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
            select(Trip)
            .where(Trip.mechanic_id == mechanic.id)
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.user))
        )
        return session.exec(statement).all()

    elif current_user.role == "user":
        # If the logged-in user is a customer, fetch their mechanic requests
        statement = (
            select(Trip)
            .where(Trip.user_id == current_user.id)
            .where(Trip.hiring_type == "Mechanic Service")
            .order_by(desc(Trip.booking_time))
            .options(
                selectinload(Trip.mechanic)
            )
        )
        return session.exec(statement).all()

    else:
        return []


@router.post("/{trip_id}/cancel")
def cancel_mechanic_trip(
    trip_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    trip = session.get(Trip, trip_id)
    if not trip or trip.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trip not found")

    if trip.status in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=400, detail="Cannot cancel a completed or cancelled trip"
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

    if mechanic_user_id_to_notify:
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[mechanic_user_id_to_notify],
            title="Service Cancelled ❌",
            body="The customer has cancelled this mechanic request.",
            data={"trip_id": trip.id, "type": "cancellation"},
        )

    return {"message": "Mechanic trip cancelled successfully"}


@router.get("/mechanic/offers", response_model=List[TripOfferPublic])
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
        statement = select(Trip).where(Trip.id == offer.trip_id).with_for_update()
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
        data={"trip_id": trip.id, "screen": "tracking"},
    )

    return {"message": "Service accepted", "trip_id": trip.id}


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
    trip = session.get(Trip, offer.trip_id)
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
