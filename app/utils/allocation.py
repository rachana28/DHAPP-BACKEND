from sqlmodel import Session, select, func, desc
from datetime import datetime, timedelta
from typing import List
from app.models import Driver, Trip, TripOffer


def get_driver_score(
    driver: Driver, last_trip_time: datetime, active_offers_count: int
) -> float:
    """
    Calculates a score (Intelligence Algorithm) for a driver.

    Factors:
    - Rating (Weighted high)
    - Recency (Waiting longer = Higher priority)
    - Active Offers (Already busy = Lower priority/Penalty)
    """

    # 1. Base Score from Rating (0-50 points)
    score = (driver.rating or 0) * 10

    # 2. Recency Bonus (Up to 40 points)
    if last_trip_time:
        hours_since_last = (datetime.utcnow() - last_trip_time).total_seconds() / 3600

        if hours_since_last > 168:  # > 1 week
            score += 40
        elif hours_since_last > 72:  # > 3 days
            score += 30
        elif hours_since_last > 24:  # > 1 day
            score += 20
        elif hours_since_last > 4:  # > 4 hours
            score += 10
    else:
        # New driver or no history -> High priority to engage them
        score += 50

    # 3. Load Penalty (Fairness Logic)
    # If driver has other pending offers, reduce score to give others a chance.
    if active_offers_count > 0:
        score -= active_offers_count * 25

    return score


def rank_drivers(session: Session, vehicle_type: str) -> List[Driver]:
    """
    Returns drivers matching the vehicle type, ranked by the algorithm.
    """
    # Filter by vehicle and availability
    query = select(Driver).where(
        Driver.vehicle_type == vehicle_type, Driver.status == "available"
    )
    drivers = session.exec(query).all()

    driver_scores = []

    for driver in drivers:
        # Get Last Trip Time
        last_trip = session.exec(
            select(Trip.booking_time)
            .where(Trip.driver_id == driver.id)
            .order_by(desc(Trip.booking_time))
            .limit(1)
        ).first()

        # Get Current Active Offers (Pending)
        active_offers = session.exec(
            select(func.count(TripOffer.id)).where(
                TripOffer.driver_id == driver.id, TripOffer.status == "pending"
            )
        ).one()

        score = get_driver_score(driver, last_trip, active_offers)
        driver_scores.append((driver, score))

    # Sort by score descending (Higher score = Better match)
    driver_scores.sort(key=lambda x: x[1], reverse=True)

    return [d[0] for d in driver_scores]


def create_offers_for_tier(
    session: Session, trip_id: int, drivers: List[Driver], tier: int
):
    """
    Generates TripOffer records for the specified list of drivers.
    """
    for driver in drivers:
        offer = TripOffer(
            trip_id=trip_id, driver_id=driver.id, status="pending", tier=tier
        )
        session.add(offer)
    session.commit()


def attempt_trip_escalation(session: Session, trip: Trip) -> bool:
    """
    Single Trip Escalation Logic.
    Checks if a trip should move to the next tier or be cancelled.
    Returns True if an action was taken (escalated or cancelled).
    """
    TIER_SIZE = 3

    # 1. Determine Current Tier
    latest_offer = session.exec(
        select(TripOffer)
        .where(TripOffer.trip_id == trip.id)
        .order_by(desc(TripOffer.tier))
        .limit(1)
    ).first()

    if not latest_offer:
        return False

    current_tier = latest_offer.tier
    should_escalate = False

    # 2. Check Conditions
    # Condition A: Time Threshold (10 mins)
    if (datetime.utcnow() - latest_offer.created_at) > timedelta(minutes=10):
        should_escalate = True

    # Condition B: All rejected/processed in current tier
    pending_in_tier = session.exec(
        select(func.count(TripOffer.id)).where(
            TripOffer.trip_id == trip.id,
            TripOffer.tier == current_tier,
            TripOffer.status == "pending",
        )
    ).one()

    if pending_in_tier == 0:
        should_escalate = True

    # 3. Execute Escalation
    if should_escalate:
        # Double Check: Did anyone accept?
        accepted_count = session.exec(
            select(func.count(TripOffer.id)).where(
                TripOffer.trip_id == trip.id,
                TripOffer.tier == current_tier,
                TripOffer.status == "accepted",
            )
        ).one()
        if accepted_count > 0:
            return False  # Trip is taken, don't escalate

        next_tier = current_tier + 1
        all_ranked_drivers = rank_drivers(session, trip.vehicle_type)

        # Calculate Next Batch
        start = current_tier * TIER_SIZE
        end = start + TIER_SIZE
        next_batch = all_ranked_drivers[start:end]

        if next_batch:
            # Clean up old pending offers (if any remain)
            old_offers = session.exec(
                select(TripOffer).where(
                    TripOffer.trip_id == trip.id, TripOffer.status == "pending"
                )
            ).all()
            for o in old_offers:
                session.delete(o)

            create_offers_for_tier(session, trip.id, next_batch, next_tier)
            return True
        else:
            # --- MISSING LOGIC FIXED HERE ---
            # No drivers left? Auto-Cancel the trip.
            trip.status = "cancelled"
            session.add(trip)
            # Optionally: Clean up offers so it doesn't clutter DB
            all_offers = session.exec(
                select(TripOffer).where(TripOffer.trip_id == trip.id)
            ).all()
            for o in all_offers:
                session.delete(o)

            return True

    return False


def process_tier_escalation(session: Session) -> int:
    """
    Background Task: Scans all searching trips.
    """
    active_trips = session.exec(select(Trip).where(Trip.status == "searching")).all()
    count = 0
    for trip in active_trips:
        if attempt_trip_escalation(session, trip):
            count += 1
    session.commit()
    return count
