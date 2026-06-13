"""Center-member ⇄ service-booking assignment engine.

Single source of truth for:
  * picking/auto-assigning a member to a ``ServiceRequest``,
  * the candidate query the center's "assign" picker uses,
  * member active-assignment / stats helpers (completed / pending / hours),
  * active-cache invalidation.

Assignment rules (locked):
  * Eligible member ≡ ``status == "approved" AND is_online``.
  * Expertise is HARD for auto-assign (member.expert_in must contain the
    booking's service_name); if none are free the booking is left unassigned and
    the center is notified. Manual assignment is SOFT (center may override).
  * Auto-assign prefers an idle member (zero active tasks); if all are busy it
    picks the one whose earliest active task finishes soonest.
  * "Hours worked" = actual elapsed: completed_time − work_start, where
    work_start = checked_in_time (walk-in) || slot.start_time (slot) ||
    assigned_at (fallback).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.core import cache
from app.core.models import (
    BookingType,
    CenterMember,
    CenterService,
    ServiceCenter,
    ServiceRequest,
    ServiceSlot,
    ServiceStatus,
)
from app.utils.booking_states import SERVICE_ACTIVE_STATES
from app.utils.notifications import notify_safe


# ── small value helpers ──────────────────────────────────────────────────────
def _status_value(status) -> str:
    return getattr(status, "value", status) or ""


def _combine(d, t) -> Optional[datetime]:
    """Combine a date + ``"HH:MM"`` string into a datetime (None if either is
    missing / malformed)."""
    if not d or not t:
        return None
    try:
        parts = str(t).split(":")
        return datetime(d.year, d.month, d.day, int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


# ── candidate / availability queries ─────────────────────────────────────────
def approved_online_members(
    session: Session, center_id: int
) -> List[CenterMember]:
    """All members of ``center_id`` that are eligible for assignment
    (approved + online)."""
    return list(
        session.exec(
            select(CenterMember).where(
                CenterMember.service_center_id == center_id,
                CenterMember.status == "approved",
                CenterMember.is_online == True,  # noqa: E712
            )
        ).all()
    )


def expertise_matches(member: CenterMember, service_name: Optional[str]) -> bool:
    return bool(service_name) and service_name in (member.expert_in or [])


def member_active_assignments(
    session: Session, member_id: int
) -> List[ServiceRequest]:
    """The member's currently-engaged (non-terminal) assignments."""
    return list(
        session.exec(
            select(ServiceRequest).where(
                ServiceRequest.assigned_member_id == member_id,
                ServiceRequest.status.in_(SERVICE_ACTIVE_STATES),
            )
        ).all()
    )


def _lifetime_count(session: Session, member_id: int) -> int:
    return len(
        session.exec(
            select(ServiceRequest.id).where(
                ServiceRequest.assigned_member_id == member_id
            )
        ).all()
    )


def completion_estimate(
    session: Session, booking: ServiceRequest
) -> Optional[datetime]:
    """Best estimate of when ``booking`` will be done — used to rank busy
    members. expected_return → slot.end_time → assigned/booking time + service
    duration."""
    est = _combine(booking.expected_return_date, booking.expected_return_time)
    if est:
        return est
    if booking.slot_id:
        slot = session.get(ServiceSlot, booking.slot_id)
        if slot and slot.end_time:
            return slot.end_time
    service = session.get(CenterService, booking.center_service_id)
    hours = (service.service_duration_hours if service else None) or 2.0
    base = booking.assigned_at or booking.booking_time or datetime.utcnow()
    return base + timedelta(hours=hours)


def member_soonest_free_at(session: Session, member_id: int) -> Optional[datetime]:
    ests = [
        completion_estimate(session, b)
        for b in member_active_assignments(session, member_id)
    ]
    ests = [e for e in ests if e]
    return min(ests) if ests else None


# ── stats + sanitized blocks for the center views ────────────────────────────
def assignment_work_start(
    session: Session, booking: ServiceRequest
) -> Optional[datetime]:
    """When the member effectively began work on ``booking``."""
    if booking.checked_in_time:
        return booking.checked_in_time
    if booking.slot_id:
        slot = session.get(ServiceSlot, booking.slot_id)
        if slot and slot.start_time:
            return slot.start_time
    return booking.assigned_at


def member_stats(session: Session, member_id: int) -> Dict[str, Any]:
    """Completed / pending counts + total actual hours worked for a member."""
    rows = list(
        session.exec(
            select(ServiceRequest).where(
                ServiceRequest.assigned_member_id == member_id
            )
        ).all()
    )
    completed = [b for b in rows if _status_value(b.status) == "completed"]
    pending = [b for b in rows if _status_value(b.status) in SERVICE_ACTIVE_STATES]

    total_hours = 0.0
    for b in completed:
        start = assignment_work_start(session, b)
        if start and b.completed_time:
            delta = (b.completed_time - start).total_seconds() / 3600.0
            if delta > 0:
                total_hours += delta

    return {
        "total_completed": len(completed),
        "total_pending": len(pending),
        "total_hours_worked": round(total_hours, 1),
    }


def member_current_assignment_blocks(
    session: Session, member_id: int
) -> List[Dict[str, Any]]:
    """Sanitized "what they're working on now" blocks for the center's member
    list — no user / price / payment data."""
    blocks: List[Dict[str, Any]] = []
    for b in member_active_assignments(session, member_id):
        blocks.append(
            {
                "booking_id": b.reference_id,
                "service_name": b.service_name,
                "vehicle_type": b.vehicle_type,
                "status": _status_value(b.status),
                "expected_return_date": b.expected_return_date,
                "expected_return_time": b.expected_return_time,
            }
        )
    return blocks


# ── cache invalidation ───────────────────────────────────────────────────────
def invalidate_member_active_cache(member_id: int) -> None:
    cache.cache_delete(cache.active_key("center_member", member_id))


def invalidate_center_active_cache(center_id: int) -> None:
    cache.cache_delete(cache.active_key("service_center", center_id))


# ── notifications ────────────────────────────────────────────────────────────
def _notify_member_assigned(
    session: Session, booking: ServiceRequest, member: CenterMember, *, auto: bool
) -> None:
    notify_safe(
        session=session,
        user_ids=[member.user_id],
        title="New assignment",
        body=f"You have been assigned to {booking.service_name} "
        f"(booking {booking.reference_id}).",
        data={
            "type": "service_assignment",
            "booking_id": booking.reference_id,
            "auto": auto,
        },
    )


def _notify_center_no_member(
    session: Session, center_id: int, booking: ServiceRequest
) -> None:
    center = session.get(ServiceCenter, center_id)
    if not center:
        return
    notify_safe(
        session=session,
        user_ids=[center.user_id],
        title="Assign a member",
        body=f"No available member is skilled for {booking.service_name}. "
        f"Please assign one manually for booking {booking.reference_id}.",
        data={
            "type": "service_assignment_needed",
            "booking_id": booking.reference_id,
        },
    )


# ── the engine ───────────────────────────────────────────────────────────────
def auto_assign_member(
    session: Session,
    booking: ServiceRequest,
    *,
    commit: bool = True,
    notify_center_on_no_match: bool = True,
) -> Optional[CenterMember]:
    """Auto-assign an eligible expert member to ``booking`` if it is still
    unassigned. Returns the chosen member, or None (already assigned / walk-in
    not yet arrived / no expert available — center notified in the last case,
    unless ``notify_center_on_no_match`` is False, e.g. the periodic sweep, to
    avoid re-pinging the center every cycle)."""
    # Serialize concurrent auto-assigners (check-in hook, status hook, sweep) on
    # this booking's row: a FOR UPDATE refresh locks the row and reloads it, so
    # the assigned_member_id check below reflects any assignment a racing caller
    # committed first (otherwise two callers could each assign a member). It is a
    # real row lock on Postgres and a harmless no-op on SQLite; the lock is held
    # until the session.commit() at the end of this function releases it. A
    # manual assignment that races will simply block on this lock, then overwrite
    # (center's explicit choice wins) — no lost update.
    if booking.id is not None:
        try:
            session.refresh(booking, with_for_update=True)
        except Exception:
            # Booking vanished (shouldn't happen — bookings are never deleted,
            # only cancelled) — nothing to assign.
            return None

    if booking.assigned_member_id:
        return None

    service_name = booking.service_name
    center_id = booking.service_center_id

    # Walk-in bookings are only auto-assigned once the vehicle has checked in
    # (status `accepted` means booked-but-not-arrived). Slot bookings have no
    # such gate. This keeps the rule in one place regardless of the caller
    # (check-in hook, status hook, or the sweep).
    service = session.get(CenterService, booking.center_service_id)
    if (
        service
        and service.booking_type == BookingType.WALK_IN
        and _status_value(booking.status) == ServiceStatus.ACCEPTED.value
    ):
        return None

    candidates = [
        m
        for m in approved_online_members(session, center_id)
        if expertise_matches(m, service_name)  # HARD expertise for auto-assign
    ]
    if not candidates:
        if notify_center_on_no_match:
            _notify_center_no_member(session, center_id, booking)
        return None

    active_map = {m.id: member_active_assignments(session, m.id) for m in candidates}
    idle = [m for m in candidates if not active_map[m.id]]

    if idle:
        # Load-balance: fewest lifetime assignments, then lowest id (stable).
        lifetime = {m.id: _lifetime_count(session, m.id) for m in idle}
        chosen = min(idle, key=lambda m: (lifetime[m.id], m.id))
    else:
        # All busy → the one whose earliest active task finishes soonest.
        def _soonest(m: CenterMember) -> datetime:
            ests = [completion_estimate(session, b) for b in active_map[m.id]]
            ests = [e for e in ests if e]
            return min(ests) if ests else datetime.max

        chosen = min(candidates, key=lambda m: (_soonest(m), m.id))

    booking.assigned_member_id = chosen.id
    booking.assigned_at = datetime.utcnow()
    booking.auto_assigned = True
    session.add(booking)
    if commit:
        session.commit()
        session.refresh(booking)

    _notify_member_assigned(session, booking, chosen, auto=True)
    invalidate_member_active_cache(chosen.id)
    invalidate_center_active_cache(center_id)
    return chosen
