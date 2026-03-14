from sqlmodel import Session, select, func, desc
from datetime import datetime, timedelta
from typing import List
from app.core.models import TowTruckDriver, Trip, TowTripOffer
from app.utils.notifications import send_push_notification


def get_tow_driver_score(
    driver: TowTruckDriver, last_trip_time: datetime, active_offers_count: int
) -> float:
    score = (driver.rating or 0) * 10
    if last_trip_time:
        hours_since_last = (datetime.utcnow() - last_trip_time).total_seconds() / 3600
        if hours_since_last > 24:
            score += 20
        elif hours_since_last > 4:
            score += 10
    else:
        score += 50

    if active_offers_count > 0:
        score -= active_offers_count * 25
    return score


def rank_tow_drivers(session: Session) -> List[TowTruckDriver]:
    query = select(TowTruckDriver).where(TowTruckDriver.status == "available")
    drivers = session.exec(query).all()

    driver_scores = []
    for driver in drivers:
        last_trip = session.exec(
            select(Trip.booking_time)
            .where(Trip.tow_truck_driver_id == driver.id)
            .order_by(desc(Trip.booking_time))
            .limit(1)
        ).first()

        active_offers = session.exec(
            select(func.count(TowTripOffer.id)).where(
                TowTripOffer.tow_truck_driver_id == driver.id,
                TowTripOffer.status == "pending",
            )
        ).one()

        score = get_tow_driver_score(driver, last_trip, active_offers)
        driver_scores.append((driver, score))

    driver_scores.sort(key=lambda x: x[1], reverse=True)
    return [d[0] for d in driver_scores]


def create_tow_offers_for_tier(
    session: Session, trip_id: int, drivers: List[TowTruckDriver], tier: int
):
    for driver in drivers:
        offer = TowTripOffer(
            trip_id=trip_id, tow_truck_driver_id=driver.id, status="pending", tier=tier
        )
        session.add(offer)
    session.commit()

    # 2. Notify Drivers (Bulk Send)
    # Collect all user_ids for these drivers
    driver_user_ids = [d.user_id for d in drivers]

    if driver_user_ids:
        # Note: We cannot use BackgroundTasks easily here since this is a helper function.
        # But `send_push_notification` is optimized to batch requests.
        send_push_notification(
            session=session,
            user_ids=driver_user_ids,
            title="New Tow Request! 🚨",
            body="A new customer nearby needs a tow.",
            data={"trip_id": trip_id, "type": "new_request"},
        )


def attempt_tow_trip_escalation(session: Session, trip: Trip) -> bool:
    """
    Checks if a tow trip should move to the next tier or be cancelled.
    """
    TIER_SIZE = 3

    latest_offer = session.exec(
        select(TowTripOffer)
        .where(TowTripOffer.trip_id == trip.id)
        .order_by(desc(TowTripOffer.tier))
        .limit(1)
    ).first()

    if not latest_offer:
        return False

    current_tier = latest_offer.tier
    should_escalate = False

    # Condition A: Time Threshold (10 mins)
    if (datetime.utcnow() - latest_offer.created_at) > timedelta(minutes=10):
        should_escalate = True

    # Condition B: All rejected/processed in current tier
    pending_in_tier = session.exec(
        select(func.count(TowTripOffer.id)).where(
            TowTripOffer.trip_id == trip.id,
            TowTripOffer.tier == current_tier,
            TowTripOffer.status == "pending",
        )
    ).one()

    if pending_in_tier == 0:
        should_escalate = True

    if should_escalate:
        # Check if already accepted
        accepted_count = session.exec(
            select(func.count(TowTripOffer.id)).where(
                TowTripOffer.trip_id == trip.id,
                TowTripOffer.tier == current_tier,
                TowTripOffer.status == "accepted",
            )
        ).one()
        if accepted_count > 0:
            return False

        next_tier = current_tier + 1
        all_ranked_drivers = rank_tow_drivers(session)

        start = current_tier * TIER_SIZE
        end = start + TIER_SIZE
        next_batch = all_ranked_drivers[start:end]

        if next_batch:
            # Delete old pending offers
            old_offers = session.exec(
                select(TowTripOffer).where(
                    TowTripOffer.trip_id == trip.id, TowTripOffer.status == "pending"
                )
            ).all()
            for o in old_offers:
                session.delete(o)

            create_tow_offers_for_tier(session, trip.id, next_batch, next_tier)
            return True
        else:
            # Auto-Cancel if no drivers left
            trip.status = "cancelled"
            session.add(trip)
            # Cleanup offers
            all_offers = session.exec(
                select(TowTripOffer).where(TowTripOffer.trip_id == trip.id)
            ).all()
            for o in all_offers:
                session.delete(o)
            return True

    return False


def process_tow_tier_escalation(session: Session) -> int:
    """
    Background Task: Scans all searching tow trips.
    """
    # Filter for trips that are 'searching' and have hiring_type 'Tow Service'
    # (or check existing TowTripOffers to distinguish)
    active_trips = session.exec(
        select(Trip).where(
            Trip.status == "searching", Trip.hiring_type == "Tow Service"
        )
    ).all()
    count = 0
    for trip in active_trips:
        if attempt_tow_trip_escalation(session, trip):
            count += 1
    session.commit()
    return count
