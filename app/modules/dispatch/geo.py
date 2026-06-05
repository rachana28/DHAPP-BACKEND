"""Spatial (K-Nearest-Neighbour) dispatch for tow & mechanic bookings.

Finds the closest *available* providers to a pickup point.

- On **PostgreSQL** this uses the PostGIS ``<->`` KNN operator + ``ST_DWithin``
  against the generated ``current_location`` GEOGRAPHY(Point,4326) column (which
  is GiST-indexed, see the manual migration). Both operators are index-aware.
- On **SQLite** (local dev, no PostGIS) it falls back to an in-Python Haversine
  sort over the plain ``current_lat`` / ``current_lng`` columns.

Returns provider ORM objects in closest-first order so the existing
offer/tier/escalation pipeline consumes them unchanged. Returns ``None`` when no
pickup coordinates are supplied — the caller then falls back to the legacy score
ranking, so nothing breaks for bookings without coordinates.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import List, Optional

from sqlalchemy import text
from sqlmodel import Session, select

from app.core.models import (
    Mechanic,
    MechanicTrip,
    SystemConfig,
    TowTrip,
    TowTruckDriver,
)
from app.utils.time_utils import now_ist

# --- SystemConfig keys + defaults (admin-tunable via /admin/system-config) ---
KNN_LIMIT_KEY = "dispatch_knn_limit"
KNN_SEARCH_RADIUS_M_KEY = "dispatch_knn_search_radius_m"
LOCATION_FRESHNESS_MIN_KEY = "dispatch_location_freshness_min"
GEOFENCE_RADIUS_M_KEY = "geofence_radius_m"
BOOKING_OTP_EXPIRY_MIN_KEY = "booking_otp_expiry_min"
ADDRESS_EDIT_WINDOW_MIN_KEY = "booking_address_edit_window_min"

DEFAULT_KNN_LIMIT = 5  # tier-1 size: the "5 closest" offered first
DEFAULT_KNN_SEARCH_RADIUS_M = 15000.0  # 15 km candidate search radius
DEFAULT_LOCATION_FRESHNESS_MIN = 5.0
DEFAULT_GEOFENCE_RADIUS_M = 200.0
DEFAULT_BOOKING_OTP_EXPIRY_MIN = 30.0
DEFAULT_ADDRESS_EDIT_WINDOW_MIN = 3.0

DISPATCH_POOL_LIMIT = 50

# Booking states in which an assigned provider is busy (excluded from dispatch).
TOW_ACTIVE_STATES = ("accepted", "arrived", "in_progress", "near_destination")
# "in_progress" added with the split mechanic flow (arrive → complete) so a
# mechanic actively on a job isn't offered new work.
MECHANIC_ACTIVE_STATES = ("accepted", "arrived", "in_progress")


# ── config helpers ───────────────────────────────────────────────────────────
def get_config_float(session: Session, key: str, default: float) -> float:
    cfg = session.get(SystemConfig, key)
    if cfg and cfg.value:
        try:
            return float(cfg.value)
        except (TypeError, ValueError):
            pass
    return default


def get_config_int(session: Session, key: str, default: int) -> int:
    return int(get_config_float(session, key, float(default)))


# ── geometry ─────────────────────────────────────────────────────────────────
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _is_postgres(session: Session) -> bool:
    try:
        return session.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


def _in_clause(states) -> str:
    # `states` are module-level constants (never user input) → safe to inline.
    return ", ".join("'" + s + "'" for s in states)


# ── shared nearest-provider query ────────────────────────────────────────────
def _nearest(
    session: Session,
    *,
    model,
    table: str,
    fk_col: str,
    fk_attr: str,
    trip_model,
    active_states,
    lat: Optional[float],
    lng: Optional[float],
    limit: Optional[int],
    type_col: Optional[str] = None,
    type_val: Optional[str] = None,
) -> Optional[List]:
    if lat is None or lng is None:
        return None
    # Strict provider-type filter (e.g. tow_vehicle_type) when requested.
    type_filter = (type_col, type_val) if type_col and type_val else None

    if limit is None:
        limit = get_config_int(session, KNN_LIMIT_KEY, DEFAULT_KNN_LIMIT)
    radius_m = get_config_float(
        session, KNN_SEARCH_RADIUS_M_KEY, DEFAULT_KNN_SEARCH_RADIUS_M
    )
    fresh_min = get_config_float(
        session, LOCATION_FRESHNESS_MIN_KEY, DEFAULT_LOCATION_FRESHNESS_MIN
    )
    # Compare against an IST-naive cutoff to match how the worker stamps
    # location_updated_at (avoids any timestamptz vs naive mismatch).
    fresh_cutoff = now_ist() - timedelta(minutes=fresh_min)

    if _is_postgres(session):
        ordered_ids = _knn_postgres(
            session,
            table=table,
            fk_col=fk_col,
            trip_table=trip_model.__tablename__,
            active_states=active_states,
            lat=lat,
            lng=lng,
            radius_m=radius_m,
            fresh_cutoff=fresh_cutoff,
            limit=limit,
            type_filter=type_filter,
        )
        if not ordered_ids:
            return []
        rows = session.exec(select(model).where(model.id.in_(ordered_ids))).all()
        by_id = {r.id: r for r in rows}
        return [by_id[i] for i in ordered_ids if i in by_id]

    # --- SQLite (local dev) fallback: Haversine in Python ---
    return _nearest_haversine(
        session,
        model=model,
        fk_attr=fk_attr,
        trip_model=trip_model,
        active_states=active_states,
        lat=lat,
        lng=lng,
        fresh_cutoff=fresh_cutoff,
        limit=limit,
        type_filter=type_filter,
    )


def _knn_postgres(
    session: Session,
    *,
    table: str,
    fk_col: str,
    trip_table: str,
    active_states,
    lat: float,
    lng: float,
    radius_m: float,
    fresh_cutoff,
    limit: int,
    type_filter: Optional[tuple] = None,
) -> List[int]:
    params = {
        "lat": lat,
        "lng": lng,
        "radius_m": radius_m,
        "fresh_cutoff": fresh_cutoff,
        "limit": limit,
    }
    # Optional strict provider-type clause; column name is a module-supplied
    # constant (never user input), the value is bound as a parameter.
    type_clause = ""
    if type_filter:
        type_col, type_val = type_filter
        type_clause = f"AND {type_col} = :type_val"
        params["type_val"] = type_val

    sql = text(
        f"""
        SELECT id AS pid
        FROM {table}
        WHERE status = 'available'
          AND current_location IS NOT NULL
          AND location_updated_at >= :fresh_cutoff
          {type_clause}
          AND ST_DWithin(
                current_location,
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                :radius_m
          )
          AND id NOT IN (
                SELECT {fk_col} FROM {trip_table}
                WHERE status IN ({_in_clause(active_states)})
                  AND {fk_col} IS NOT NULL
          )
        ORDER BY current_location <-> ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
        LIMIT :limit
        """
    )
    rows = session.execute(sql, params).all()
    return [r[0] for r in rows]


def _nearest_haversine(
    session: Session,
    *,
    model,
    fk_attr: str,
    trip_model,
    active_states,
    lat: float,
    lng: float,
    fresh_cutoff,
    limit: int,
    type_filter: Optional[tuple] = None,
) -> List:
    conditions = [
        model.status == "available",
        model.current_lat.is_not(None),
        model.current_lng.is_not(None),
        model.location_updated_at.is_not(None),
        model.location_updated_at >= fresh_cutoff,
    ]
    if type_filter:
        type_col, type_val = type_filter
        conditions.append(getattr(model, type_col) == type_val)
    candidates = session.exec(select(model).where(*conditions)).all()

    busy_ids = set(
        session.exec(
            select(getattr(trip_model, fk_attr)).where(
                trip_model.status.in_(active_states),
                getattr(trip_model, fk_attr).is_not(None),
            )
        ).all()
    )

    scored = [
        (p, haversine_m(lat, lng, p.current_lat, p.current_lng))
        for p in candidates
        if p.id not in busy_ids
    ]
    scored.sort(key=lambda t: t[1])
    return [p for p, _ in scored[:limit]]


# ── public API ───────────────────────────────────────────────────────────────
def nearest_available_tow_drivers(
    session: Session,
    lat: Optional[float],
    lng: Optional[float],
    limit: Optional[int] = None,
    tow_vehicle_type: Optional[str] = None,
) -> Optional[List[TowTruckDriver]]:
    """Closest available tow drivers to (lat, lng); None if no coords given.

    When ``tow_vehicle_type`` is given, only drivers of that exact tow-truck
    class are returned (strict matching).
    """
    return _nearest(
        session,
        model=TowTruckDriver,
        table="towtruckdriver",
        fk_col="tow_truck_driver_id",
        fk_attr="tow_truck_driver_id",
        trip_model=TowTrip,
        active_states=TOW_ACTIVE_STATES,
        lat=lat,
        lng=lng,
        limit=limit,
        type_col="tow_vehicle_type" if tow_vehicle_type else None,
        type_val=tow_vehicle_type,
    )


def nearest_available_mechanics(
    session: Session,
    lat: Optional[float],
    lng: Optional[float],
    limit: Optional[int] = None,
) -> Optional[List[Mechanic]]:
    """Closest available mechanics to (lat, lng); None if no coords given."""
    return _nearest(
        session,
        model=Mechanic,
        table="mechanic",
        fk_col="mechanic_id",
        fk_attr="mechanic_id",
        trip_model=MechanicTrip,
        active_states=MECHANIC_ACTIVE_STATES,
        lat=lat,
        lng=lng,
        limit=limit,
    )
