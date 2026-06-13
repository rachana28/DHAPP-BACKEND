"""Shared rating/review primitives used by every service's review endpoints.

Two kinds of rating exist per booking:

* the **service rating** — the customer's rating of the app's service itself.
  It is recorded in the unified ``ServiceRating`` table (one row per booking per
  user) so admin can see service satisfaction across all 4 services in one place.
* the **provider rating** — the customer's rating of the driver / tow-driver /
  mechanic / service-center / center-member. It stays in that provider's own
  per-provider review table and is averaged back into the provider profile's
  ``.rating`` column (and ``total_reviews`` when that column exists).

All review endpoints share the same ``RatingIn`` JSON body and are gated on the
booking having reached a rateable (completed) status — see ``RATEABLE_STATUSES``.
"""

from __future__ import annotations

from typing import Optional, Type

import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.models import ServiceRating

# Statuses at which the customer may rate, per service. Read by the user app off
# the booking's ``/summary`` ``status`` to decide when to show the rating UI.
RATEABLE_STATUSES: dict[str, set[str]] = {
    "trip": {"completed", "auto_completed", "settled"},
    "tow": {"completed"},
    "transport": {"completed"},
    "mechanic": {"completed"},
    "service_center": {"completed"},
}


class RatingIn(BaseModel):
    """The single request body shared by every rating endpoint."""

    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


def ensure_rateable(service_type: str, status) -> None:
    """Raise 400 unless ``status`` is a rateable (completed) state for the service."""
    allowed = RATEABLE_STATUSES.get(service_type, {"completed"})
    if (getattr(status, "value", status) or "") not in allowed:
        raise HTTPException(
            status_code=400, detail="Can only review completed bookings"
        )


def record_service_rating(
    session: Session,
    *,
    service_type: str,
    booking_id: int,
    booking_reference_id: str,
    user_id: uuid.UUID,
    payload: RatingIn,
) -> dict:
    """Insert the app/service rating for a booking (one per user per booking).

    Does NOT touch any provider aggregate — this is purely the app's own score.
    Raises 400 if this user already rated this booking's service.
    """
    existing = session.exec(
        select(ServiceRating).where(
            ServiceRating.service_type == service_type,
            ServiceRating.booking_id == booking_id,
            ServiceRating.user_id == user_id,
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=400, detail="You have already rated this service"
        )

    session.add(
        ServiceRating(
            service_type=service_type,
            booking_id=booking_id,
            booking_reference_id=booking_reference_id,
            user_id=user_id,
            rating=payload.rating,
            comment=payload.comment,
        )
    )
    session.commit()
    return {"message": "Rating submitted successfully", "rating": payload.rating}


def recompute_provider_average(
    session: Session,
    review_model: Type,
    fk_field: str,
    fk_value: int,
    profile_obj,
) -> None:
    """Recompute ``profile_obj.rating`` (and ``total_reviews`` when present) from
    all of the provider's reviews. Mirrors the inline logic in the service-center
    and center-member review endpoints. Caller commits."""
    reviews = session.exec(
        select(review_model).where(getattr(review_model, fk_field) == fk_value)
    ).all()
    if not reviews:
        return
    profile_obj.rating = round(sum(r.rating for r in reviews) / len(reviews), 1)
    if hasattr(profile_obj, "total_reviews"):
        profile_obj.total_reviews = len(reviews)
    session.add(profile_obj)
