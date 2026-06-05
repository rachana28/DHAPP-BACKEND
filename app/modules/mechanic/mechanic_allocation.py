from sqlmodel import Session, select, func, desc
from datetime import datetime, timedelta
from typing import List, Optional
from app.core.models import Mechanic, MechanicTrip, MechanicOffer
from app.utils.notifications import send_push_notification
from app.utils.time_utils import now_ist
from app.modules.dispatch import geo


def get_mechanic_score(
    mechanic: Mechanic, last_trip_time: datetime, active_offers_count: int
) -> float:
    score = (mechanic.rating or 0) * 10
    if last_trip_time:
        hours_since_last = (now_ist() - last_trip_time).total_seconds() / 3600
        if hours_since_last > 24:
            score += 20
        elif hours_since_last > 4:
            score += 10
    else:
        score += 50

    if active_offers_count > 0:
        score -= active_offers_count * 25
    return score


def rank_mechanics(
    session: Session,
    pickup_lat: Optional[float] = None,
    pickup_lng: Optional[float] = None,
) -> List[Mechanic]:
    """Available mechanics, closest-first.

    When pickup coordinates are supplied, ordering is by spatial distance
    (PostGIS KNN, Haversine fallback on SQLite). If no coordinates are given,
    falls back to the legacy score-based ranking so existing behaviour holds.
    """
    if pickup_lat is not None and pickup_lng is not None:
        nearby = geo.nearest_available_mechanics(
            session, pickup_lat, pickup_lng, limit=geo.DISPATCH_POOL_LIMIT
        )
        # Use spatial ordering when it yields candidates. If it's empty (e.g. no
        # mechanic has reported a fresh location yet — common right after rollout),
        # fall through to the legacy score ranking so dispatch never hard-stalls.
        if nearby:
            return nearby

    query = select(Mechanic).where(Mechanic.status == "available")
    mechanics = session.exec(query).all()

    scores = []
    for mechanic in mechanics:
        last_trip = session.exec(
            select(MechanicTrip.booking_time)
            .where(MechanicTrip.mechanic_id == mechanic.id)
            .order_by(desc(MechanicTrip.booking_time))
            .limit(1)
        ).first()

        active_offers = session.exec(
            select(func.count(MechanicOffer.id)).where(
                MechanicOffer.mechanic_id == mechanic.id,
                MechanicOffer.status == "pending",
            )
        ).one()

        score = get_mechanic_score(mechanic, last_trip, active_offers)
        scores.append((mechanic, score))

    scores.sort(key=lambda x: x[1], reverse=True)
    return [m[0] for m in scores]


def create_mechanic_offers_for_tier(
    session: Session, trip_id: int, mechanics: List[Mechanic], tier: int
):
    for mechanic in mechanics:
        offer = MechanicOffer(
            trip_id=trip_id, mechanic_id=mechanic.id, status="pending", tier=tier
        )
        session.add(offer)
    session.commit()

    driver_user_ids = [m.user_id for m in mechanics]
    if driver_user_ids:
        send_push_notification(
            session=session,
            user_ids=driver_user_ids,
            title="New Mechanic Request! 🛠️",
            body="A vehicle nearby requires mechanical assistance.",
            data={"trip_id": trip_id, "type": "new_request"},
        )


def attempt_mechanic_trip_escalation(session: Session, trip: MechanicTrip) -> bool:
    """
    Checks if a mechanic trip should move to the next tier or be cancelled.
    """
    TIER_SIZE = geo.get_config_int(session, geo.KNN_LIMIT_KEY, geo.DEFAULT_KNN_LIMIT)

    latest_offer = session.exec(
        select(MechanicOffer)
        .where(MechanicOffer.trip_id == trip.id)
        .order_by(desc(MechanicOffer.tier))
        .limit(1)
    ).first()

    if not latest_offer:
        return False

    current_tier = latest_offer.tier
    should_escalate = False

    # Condition A: Time Threshold (e.g., 10 mins without acceptance)
    if (datetime.utcnow() - latest_offer.created_at) > timedelta(minutes=10):
        should_escalate = True

    # Condition B: All mechanics in the current tier rejected the offer
    pending_in_tier = session.exec(
        select(func.count(MechanicOffer.id)).where(
            MechanicOffer.trip_id == trip.id,
            MechanicOffer.tier == current_tier,
            MechanicOffer.status == "pending",
        )
    ).one()

    if pending_in_tier == 0:
        should_escalate = True

    if should_escalate:
        # Check if it was already accepted before escalating
        accepted_count = session.exec(
            select(func.count(MechanicOffer.id)).where(
                MechanicOffer.trip_id == trip.id,
                MechanicOffer.tier == current_tier,
                MechanicOffer.status == "accepted",
            )
        ).one()

        if accepted_count > 0:
            return False

        next_tier = current_tier + 1
        all_ranked_mechanics = rank_mechanics(session, trip.start_lat, trip.start_lng)

        start = current_tier * TIER_SIZE
        end = start + TIER_SIZE
        next_batch = all_ranked_mechanics[start:end]

        if next_batch:
            # Delete old pending offers so mechanics don't see stale requests
            old_offers = session.exec(
                select(MechanicOffer).where(
                    MechanicOffer.trip_id == trip.id, MechanicOffer.status == "pending"
                )
            ).all()
            for o in old_offers:
                session.delete(o)

            create_mechanic_offers_for_tier(session, trip.id, next_batch, next_tier)
            return True
        else:
            trip.status = "no_mechanics_found"
            session.add(trip)

            # Cleanup offers
            all_offers = session.exec(
                select(MechanicOffer).where(MechanicOffer.trip_id == trip.id)
            ).all()
            for o in all_offers:
                session.delete(o)

            try:
                send_push_notification(
                    session=session,
                    user_ids=[trip.user_id],
                    title="No Mechanics Available 😔",
                    body="We couldn't find an available mechanic nearby. Please try again.",
                    data={"trip_id": trip.reference_id, "type": "no_mechanics_found"},
                )
            except Exception as e:
                print(f"No-mechanics notification error: {e}")

            return True

    return False


def process_mechanic_tier_escalation(session: Session) -> int:
    """
    Background Task: Scans all searching mechanic trips and escalates if needed.
    (This should be run periodically via a cron job or background scheduler).
    """
    active_trips = session.exec(
        select(MechanicTrip).where(MechanicTrip.status == "searching")
    ).all()

    count = 0
    for trip in active_trips:
        if attempt_mechanic_trip_escalation(session, trip):
            count += 1

    session.commit()
    return count
