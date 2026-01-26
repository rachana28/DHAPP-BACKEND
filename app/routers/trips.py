import redis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select, desc, func
from typing import List
from datetime import datetime, timedelta
from sqlalchemy.orm import selectinload

from app.database import get_session, get_redis
from app.models import (
    Trip,
    TripCreate,
    TripOffer,
    TripOfferPublic,
    TripSafe,
    Driver,
    User,
)
from app.security import get_current_user
from app.utils.allocation import (
    rank_drivers,
    create_offers_for_tier,
    process_tier_escalation,
)

router = APIRouter(prefix="/trips", tags=["Trips"])

TIER_SIZE = 3  # Configurable: How many drivers per batch


@router.post("/book-request", response_model=TripSafe)
def create_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    trip_in: TripCreate,
):
    """
    Step 1: User creates booking. System Segregates & Offers to Tier 1.
    """
    if not trip_in.vehicle_type:
        raise HTTPException(400, "Vehicle type is required.")

    # 1. Save Trip
    trip_data = trip_in.model_dump()
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


@router.get("/my-bookings", response_model=List[TripSafe])
def get_my_bookings_as_driver(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Get trips where the current user is the assigned driver.
    """
    if current_user.role != "driver":
        raise HTTPException(status_code=403, detail="Not authorized")

    driver = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    trips = session.exec(select(Trip).where(Trip.driver_id == driver.id)).all()
    return trips


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


@router.post("/driver/accept-offer/{offer_id}")
def accept_trip_offer(
    offer_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Driver accepts a trip.
    CRITICAL: Deletes all other active offers for this trip immediately.
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

    offer.status = "rejected"
    session.add(offer)
    session.commit()

    return {"message": "Offer rejected"}


@router.post("/check-escalation")
def check_and_escalate_tiers(session: Session = Depends(get_session)):
    """
    Manual trigger endpoint (useful for testing/debugging).
    """
    count = process_tier_escalation(session)
    return {"message": f"Escalated {count} trips."}
