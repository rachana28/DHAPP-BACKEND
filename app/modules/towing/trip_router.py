from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import List
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import NoResultFound

from app.core.database import get_session
from app.core.models import (
    Trip,
    TripCreate,
    TripSafe,
    TripReadUser,
    TowTruckDriver,
    TowTripOffer,
    TripOfferPublic,
    User,
)
from app.core.security import get_current_user, get_current_active_tow_truck_driver
from app.modules.towing.tow_allocation import (
    rank_tow_drivers,
    create_tow_offers_for_tier,
    attempt_tow_trip_escalation,
)
from app.utils.notifications import send_push_notification
from fastapi import BackgroundTasks

router = APIRouter(prefix="/tow-trips", tags=["Tow Trips"])


@router.post("/book-request", response_model=TripSafe)
def create_tow_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_in: TripCreate,
):
    trip_data = trip_in.model_dump()
    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"

    if not trip_data.get("hiring_type"):
        trip_data["hiring_type"] = "Tow Service"

    db_trip = Trip.model_validate(trip_data)

    session.add(db_trip)
    session.commit()
    session.refresh(db_trip)

    ranked_drivers = rank_tow_drivers(session)

    if not ranked_drivers:
        db_trip.status = "no_drivers_found"
        session.add(db_trip)
        session.commit()
        return db_trip

    tier_1_drivers = ranked_drivers[:3]
    create_tow_offers_for_tier(session, db_trip.id, tier_1_drivers, tier=1)

    return db_trip


@router.get("/my-bookings", response_model=List[TripReadUser])
def get_my_tow_bookings(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == "tow_truck_driver":
        driver = session.exec(
            select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
        ).first()
        if not driver:
            return []

        statement = (
            select(Trip)
            .where(Trip.tow_truck_driver_id == driver.id)
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.user))
        )
        return session.exec(statement).all()

    elif current_user.role == "user":
        statement = (
            select(Trip)
            .where(Trip.user_id == current_user.id)
            .where(Trip.hiring_type == "Tow Service")
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.tow_truck_driver))
        )
        return session.exec(statement).all()

    else:
        return []


@router.post("/{trip_id}/cancel")
def cancel_tow_trip(
    trip_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Cancels a Tow Trip and removes all associated offers.
    """
    trip = session.get(Trip, trip_id)
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

    if driver_user_id_to_notify:
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[driver_user_id_to_notify],
            title="Trip Cancelled ❌",
            body="The customer has cancelled this request.",
            data={"trip_id": trip.id, "type": "cancellation"},
        )

    return {"message": "Tow trip cancelled successfully"}


@router.get("/driver/offers", response_model=List[TripOfferPublic])
def get_tow_driver_offers(
    session: Session = Depends(get_session),
    current_driver: TowTruckDriver = Depends(get_current_active_tow_truck_driver),
):
    statement = (
        select(TowTripOffer)
        .where(TowTripOffer.tow_truck_driver_id == current_driver.id)
        .where(TowTripOffer.status == "pending")
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
        # This query will WAIT if another driver is currently trying to accept the same trip
        statement = select(Trip).where(Trip.id == offer.trip_id).with_for_update()
        trip = session.exec(statement).one()
    except NoResultFound:
        raise HTTPException(404, "Trip not found")

    # Safe Status Check (Guaranteed by Lock)
    if trip.status != "searching":
        session.rollback() # Release lock immediately
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
            data={"trip_id": trip.id, "screen": "tracking"},
        )
    except Exception as e:
        print(f"Notification error: {e}")

    return {"message": "Trip accepted", "trip_id": trip.id}


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

    trip = session.get(Trip, offer.trip_id)
    if trip and trip.status == "searching":
        escalated = attempt_tow_trip_escalation(session, trip)
        if escalated:
            session.commit()

    return {"message": "Offer rejected"}
