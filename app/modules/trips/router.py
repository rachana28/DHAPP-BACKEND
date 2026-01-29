import redis
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
)
from app.core.security import get_current_user
from app.modules.trips.allocation import (
    rank_drivers,
    create_offers_for_tier,
    process_tier_escalation,
    attempt_trip_escalation,
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
        # New User Logic
        # Fetch all trips for this user, joining the Driver table to populate details
        statement = (
            select(Trip)
            .where(Trip.user_id == current_user.id)
            .order_by(desc(Trip.booking_time))
            .options(selectinload(Trip.driver))  # Load driver info for TripReadUser
        )
        return session.exec(statement).all()
    else:
        return []


@router.post("/{trip_id}/cancel")
def cancel_trip(
    trip_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    User cancels a trip.
    Logic:
    1. Mark Trip as 'cancelled'.
    2. DELETE all active TripOffers so drivers no longer see the request.
    """
    trip = session.get(Trip, trip_id)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    # Security: Ensure user owns this trip
    if trip.user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail="Not authorized to cancel this trip"
        )

    if trip.status in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel a completed or already cancelled trip",
        )

    # 1. Update Status
    trip.status = "cancelled"
    session.add(trip)

    # 2. Delete Requests (Offers)
    # This removes the "Ring" from all drivers' phones/dashboards
    offers = session.exec(select(TripOffer).where(TripOffer.trip_id == trip.id)).all()
    for offer in offers:
        session.delete(offer)

    session.commit()

    return {"message": "Trip cancelled successfully"}


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
