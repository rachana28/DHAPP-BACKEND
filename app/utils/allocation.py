from sqlmodel import Session, select, func, desc
from datetime import datetime
from typing import List, Tuple
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


def process_tier_escalation(session: Session):
    """
    Core logic to check for trips pending > 10 mins and escalate them.
    Returns the number of escalated trips.
    """
    from app.models import (
        Trip,
        TripOffer,
    )  # Import inside to avoid circular deps if any
    from datetime import datetime, timedelta

    TIER_SIZE = 3  # Ensure this matches your config

    # Find trips currently searching
    active_trips = session.exec(select(Trip).where(Trip.status == "searching")).all()
    escalated_count = 0

    for trip in active_trips:
        # Get latest offers to determine current tier
        latest_offer = session.exec(
            select(TripOffer)
            .where(TripOffer.trip_id == trip.id)
            .order_by(desc(TripOffer.tier))
            .limit(1)
        ).first()

        if not latest_offer:
            continue

        current_tier = latest_offer.tier
        should_escalate = False

        # Condition A: Time Threshold (10 mins)
        time_diff = datetime.utcnow() - latest_offer.created_at
        if time_diff > timedelta(minutes=10):
            should_escalate = True

        # Condition B: All rejected in current tier
        pending_in_tier = session.exec(
            select(func.count(TripOffer.id)).where(
                TripOffer.trip_id == trip.id,
                TripOffer.tier == current_tier,
                TripOffer.status == "pending",
            )
        ).one()

        if pending_in_tier == 0:
            should_escalate = True

        if should_escalate:
            # Prepare Next Tier
            next_tier = current_tier + 1
            all_ranked_drivers = rank_drivers(session, trip.vehicle_type)

            start_index = current_tier * TIER_SIZE
            end_index = start_index + TIER_SIZE
            next_batch = all_ranked_drivers[start_index:end_index]

            if next_batch:
                # Expire old pending offers
                old_pending = session.exec(
                    select(TripOffer).where(
                        TripOffer.trip_id == trip.id, TripOffer.status == "pending"
                    )
                ).all()
                for op in old_pending:
                    session.delete(op)

                # Create offers for next tier
                create_offers_for_tier(session, trip.id, next_batch, next_tier)
                escalated_count += 1
            else:
                # No more drivers!
                trip.status = "no_drivers_available"
                session.add(trip)

    session.commit()
    return escalated_count
