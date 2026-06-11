"""
Driver ranking + tier-based offer cascade for Trip bookings.

Each tier offers a fresh batch of three best-ranked drivers. Earlier tiers
stay alive when the next tier launches (F1) — the first driver to click
accept across any live tier locks the trip via the row-lock in
``/driver/{trip_id}/accept-and-pay``. Escalation fires on either condition:
all offers in the current tier processed, or 10 minutes elapsed since the
tier was created.
"""

from datetime import datetime, timedelta
from typing import List

from sqlmodel import Session, desc, func, select

from app.core.models import Driver, Trip, TripOffer
from app.modules.trips.trip_service import TripService
from app.utils.time_utils import now_ist

TIER_SIZE = 3
TIER_ESCALATION_AFTER = timedelta(minutes=10)


def get_driver_score(
    driver: Driver, last_trip_time: datetime, active_offers_count: int
) -> float:
    score = (driver.rating or 0) * 10

    if last_trip_time:
        hours_idle = (now_ist() - last_trip_time).total_seconds() / 3600
        if hours_idle > 168:
            score += 40
        elif hours_idle > 72:
            score += 30
        elif hours_idle > 24:
            score += 20
        elif hours_idle > 4:
            score += 10
    else:
        score += 50

    if active_offers_count > 0:
        score -= active_offers_count * 25
    return score


def rank_drivers(session: Session, vehicle_type: str) -> List[Driver]:
    now = now_ist()
    drivers = session.exec(
        select(Driver).where(
            Driver.vehicle_type == vehicle_type,
            Driver.status == "available",
            (Driver.suspended_until.is_(None)) | (Driver.suspended_until <= now),
        )
    ).all()
    if not drivers:
        return []

    busy_driver_ids = set(
        session.exec(
            select(Trip.driver_id).where(
                Trip.driver_id.isnot(None),
                Trip.status.in_(TripService.DRIVER_BUSY_STATES),
            )
        ).all()
    )

    scored: list = []
    for driver in drivers:
        if driver.id in busy_driver_ids:
            continue
        last_trip = session.exec(
            select(Trip.booking_time)
            .where(Trip.driver_id == driver.id)
            .order_by(desc(Trip.booking_time))
            .limit(1)
        ).first()
        active_offers = session.exec(
            select(func.count(TripOffer.id)).where(
                TripOffer.driver_id == driver.id, TripOffer.status == "pending"
            )
        ).one()
        scored.append((driver, get_driver_score(driver, last_trip, active_offers)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [d for d, _ in scored]


def create_offers_for_tier(
    session: Session, trip_id: int, drivers: List[Driver], tier: int
):
    for driver in drivers:
        session.add(
            TripOffer(trip_id=trip_id, driver_id=driver.id, status="pending", tier=tier)
        )
    session.commit()


def attempt_trip_escalation(session: Session, trip: Trip) -> bool:
    latest_offer = session.exec(
        select(TripOffer)
        .where(TripOffer.trip_id == trip.id)
        .order_by(desc(TripOffer.tier))
        .limit(1)
    ).first()
    if not latest_offer:
        ranked = rank_drivers(session, trip.vehicle_type)
        if ranked:
            create_offers_for_tier(session, trip.id, ranked[:TIER_SIZE], tier=1)
            return True
        return False

    current_tier = latest_offer.tier
    age = now_ist() - latest_offer.created_at
    pending_in_tier = session.exec(
        select(func.count(TripOffer.id)).where(
            TripOffer.trip_id == trip.id,
            TripOffer.tier == current_tier,
            TripOffer.status == "pending",
        )
    ).one()
    should_escalate = age > TIER_ESCALATION_AFTER or pending_in_tier == 0
    if not should_escalate:
        return False

    accepted = session.exec(
        select(func.count(TripOffer.id)).where(
            TripOffer.trip_id == trip.id,
            TripOffer.tier == current_tier,
            TripOffer.status == "accepted",
        )
    ).one()
    if accepted > 0:
        return False

    next_tier = current_tier + 1
    ranked = rank_drivers(session, trip.vehicle_type)

    already_offered = set(
        session.exec(
            select(TripOffer.driver_id).where(TripOffer.trip_id == trip.id)
        ).all()
    )
    next_batch = [d for d in ranked if d.id not in already_offered][:TIER_SIZE]

    if next_batch:
        create_offers_for_tier(session, trip.id, next_batch, next_tier)
        return True

    if ranked:
        for o in session.exec(
            select(TripOffer).where(
                TripOffer.trip_id == trip.id,
                TripOffer.status != "accepted",
            )
        ).all():
            session.delete(o)
        create_offers_for_tier(session, trip.id, ranked[:TIER_SIZE], tier=1)
        return True

    return False


def process_tier_escalation(session: Session) -> int:
    active_trips = session.exec(select(Trip).where(Trip.status == "searching")).all()
    count = 0
    for trip in active_trips:
        if attempt_trip_escalation(session, trip):
            count += 1
    session.commit()
    return count
