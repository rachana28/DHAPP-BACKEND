"""
Human-readable, non-enumerable reference IDs for the public API surface.

Every integer-PK entity that is exposed to a client keeps its integer primary
key internally (all foreign keys / joins are unchanged) but also carries a
unique ``reference_id`` string. That string is the ONLY id we put in responses
and accept in URLs, so the sequential integer PK never leaks.

Scheme: ``PREFIX + YEAR + [SUBTYPE] + SEQ`` where SEQ is zero-padded and reset
per calendar year. Example: ``TP2026ST0001`` (trip, 2026, short-term, #1).

The sequence counter lives in the ``IdSequence`` table, one row per
``(entity_type, year)``. Counters are shared across subtypes (a trip's ST/MT/OS
suffix is informational only) so the visible number is globally incremental for
that entity within the year.
"""

from __future__ import annotations

import secrets
import string
from typing import Optional, Type

from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from app.utils.time_utils import now_ist

# --- entity keys (use these constants at call sites) ---
TRIP = "trip"
TOW_TRIP = "tow_trip"
MECHANIC_TRIP = "mechanic_trip"
SERVICE_REQUEST = "service_request"
DRIVER = "driver"
TOW_DRIVER = "tow_driver"
MECHANIC = "mechanic"
SERVICE_CENTER = "service_center"
CENTER_MEMBER = "center_member"
PAYMENT = "payment"
ADDRESS = "address"
CARD = "card"
WALLET_TXN = "wallet_txn"
PROVIDER_WALLET = "provider_wallet"
PROVIDER_WALLET_TXN = "provider_wallet_txn"
MERCHANT_LEDGER = "merchant_ledger"
PAYOUT = "payout"

# entity -> (prefix, zero-pad width)
_SCHEME = {
    TRIP: ("TP", 4),
    TOW_TRIP: ("TW", 4),
    MECHANIC_TRIP: ("MC", 4),
    SERVICE_REQUEST: ("SB", 4),
    DRIVER: ("DR", 4),
    TOW_DRIVER: ("TD", 4),
    MECHANIC: ("MN", 4),
    SERVICE_CENTER: ("SC", 4),
    CENTER_MEMBER: ("CM", 4),
    PAYMENT: ("PAY", 6),
    ADDRESS: ("ADR", 4),
    CARD: ("CRD", 4),
    WALLET_TXN: ("WTX", 6),
    PROVIDER_WALLET: ("PWL", 6),
    PROVIDER_WALLET_TXN: ("PWX", 6),
    MERCHANT_LEDGER: ("MBL", 6),
    PAYOUT: ("PO", 6),
}

# Trip hiring_type -> subtype segment
_TRIP_SUBTYPE = {"daily": "ST", "short_term": "ST", "monthly": "MT", "outstation": "OS"}


def trip_subtype(hiring_type: Optional[str]) -> str:
    """Map a Trip.hiring_type to its reference-id subtype segment (defaults ST)."""
    return _TRIP_SUBTYPE.get((hiring_type or "").strip().lower(), "ST")


def _next_sequence(session: Session, entity: str, year: int) -> int:
    """Atomically claim the next counter value for (entity, year).

    Runs inside the caller's transaction (no commit here) so the counter bump
    and the row that uses it commit together. Row-level lock serializes
    concurrent callers on Postgres; on SQLite it is a harmless no-op.
    """
    from app.core.models import IdSequence

    def _fetch_locked():
        return session.exec(
            select(IdSequence)
            .where(IdSequence.entity_type == entity, IdSequence.year == year)
            .with_for_update()
        ).first()

    row = _fetch_locked()

    if row is None:
        # Create the counter row inside a SAVEPOINT so a lost create-race only
        # rolls back this insert — NOT the caller's in-progress transaction
        # (which holds the booking/profile row being assigned this id).
        try:
            with session.begin_nested():
                row = IdSequence(entity_type=entity, year=year, last_value=0)
                session.add(row)
                session.flush()
        except IntegrityError:
            row = _fetch_locked()
            if row is None:
                raise

    row.last_value += 1
    session.add(row)
    session.flush()
    return row.last_value


def generate_reference_id(
    session: Session,
    entity: str,
    *,
    subtype: str = "",
    year: Optional[int] = None,
) -> str:
    """Return the next reference id for ``entity`` (e.g. ``TP2026ST0001``)."""
    if entity not in _SCHEME:
        raise ValueError(f"Unknown reference-id entity: {entity!r}")
    prefix, pad = _SCHEME[entity]
    yr = year if year is not None else now_ist().year
    seq = _next_sequence(session, entity, yr)
    return f"{prefix}{yr}{subtype}{seq:0{pad}d}"


def get_by_reference(session: Session, model: Type, reference_id: str):
    """Fetch a row by its ``reference_id``, or None if not found."""
    return session.exec(
        select(model).where(model.reference_id == reference_id)
    ).first()


# Code alphabet for service-center codes: uppercase letters + digits. Ambiguous
# characters are kept (full A-Z0-9) so the space is large; collisions are
# handled by the DB-uniqueness retry loop below.
_CENTER_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CENTER_CODE_LEN = 6


def generate_center_code(session: Session, *, max_attempts: int = 25) -> str:
    """Return a unique 6-char uppercase-alphanumeric ``ServiceCenter.center_code``.

    Generated randomly and checked against the DB; retries on the (rare)
    collision. Raises after ``max_attempts`` exhausted (effectively never with a
    36**6 ≈ 2.1B space)."""
    from app.core.models import ServiceCenter

    for _ in range(max_attempts):
        code = "".join(
            secrets.choice(_CENTER_CODE_ALPHABET) for _ in range(_CENTER_CODE_LEN)
        )
        exists = session.exec(
            select(ServiceCenter).where(ServiceCenter.center_code == code)
        ).first()
        if not exists:
            return code
    raise RuntimeError("Could not generate a unique center_code; try again.")
