from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import List
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import NoResultFound

from app.core.database import get_session
from app.core.models import (
    TowTrip,
    TowTripCreate,
    TowTripSafe,
    TowTripReadUser,
    TowTruckDriver,
    TowTripOffer,
    TowTripOfferPublic,
    User,
)
from app.modules.payments.service import refund_booking_payments
from app.core.security import get_current_user, get_current_active_tow_truck_driver
from app.modules.towing.tow_allocation import (
    rank_tow_drivers,
    create_tow_offers_for_tier,
    attempt_tow_trip_escalation,
)
from app.utils.notifications import send_push_notification
from app.utils.id_generator import generate_reference_id, get_by_reference, TOW_TRIP
from fastapi import BackgroundTasks

router = APIRouter(prefix="/tow-trips", tags=["Tow Trips"])


@router.post("/book-request", response_model=TowTripSafe)
def create_tow_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_in: TowTripCreate,
):
    trip_data = trip_in.model_dump(exclude_unset=True)
    trip_data["user_id"] = current_user.id
    trip_data["status"] = "searching"
    trip_data.pop("hiring_type", None)  # discriminator no longer stored

    db_trip = TowTrip.model_validate(trip_data)
    db_trip.reference_id = generate_reference_id(session, TOW_TRIP)

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


@router.get("/my-bookings", response_model=List[TowTripReadUser])
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
            select(TowTrip)
            .where(TowTrip.tow_truck_driver_id == driver.id)
            .order_by(desc(TowTrip.booking_time))
            .options(selectinload(TowTrip.user))
        )
        return session.exec(statement).all()

    elif current_user.role == "user":
        statement = (
            select(TowTrip)
            .where(TowTrip.user_id == current_user.id)
            .order_by(desc(TowTrip.booking_time))
            .options(selectinload(TowTrip.tow_truck_driver))
        )
        return session.exec(statement).all()

    else:
        return []


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

    return {"message": "Trip accepted", "trip_id": trip.reference_id}


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
