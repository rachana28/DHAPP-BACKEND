"""Transport service alias routers.

Transport runs on the **same** stack/table as tow (per product decision: no new
table). Rather than duplicate the tow trip/driver logic, these routers re-expose
the existing towing endpoints under ``/transport-trips`` and
``/transport-drivers`` prefixes by copying each route onto a new router — the
underlying handler callables are reused verbatim. Behaviour is driven by the
booking row's ``service_type`` (set to "transport") and the role-aware dispatch
in ``tow_allocation`` / ``geo``.

The only specialised route is ``POST /transport-trips/book-request``, which forces
``service_type="transport"`` so the transport app never has to send it.
"""

from __future__ import annotations

from typing import Sequence

import redis
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from sqlmodel import Session

from app.core.database import get_redis, get_session
from app.core.models import TowTripCreate, TowTripSafe, User
from app.core.security import get_current_user
from app.modules.towing import driver_router as _tow_drivers
from app.modules.towing import trip_router as _tow_trips
from app.modules.towing.trip_router import create_tow_booking_request


def _alias_router(
    src: APIRouter,
    old_prefix: str,
    new_prefix: str,
    tags: Sequence[str],
    skip_paths: Sequence[str] = (),
) -> APIRouter:
    """Re-register every route of ``src`` under ``new_prefix`` (reusing the same
    endpoint callables). ``skip_paths`` lists *source* paths to omit (e.g. when an
    endpoint is overridden on the alias)."""
    alias = APIRouter(tags=list(tags))
    for route in src.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in skip_paths:
            continue
        new_path = new_prefix + route.path[len(old_prefix) :]
        alias.add_api_route(
            new_path,
            route.endpoint,
            methods=list(route.methods),
            response_model=route.response_model,
            status_code=route.status_code,
            dependencies=route.dependencies,
            summary=route.summary,
            description=route.description,
            name=route.name,
            response_model_exclude_none=route.response_model_exclude_none,
        )
    return alias


# ── /transport-trips ──────────────────────────────────────────────────────────
transport_trips_router = _alias_router(
    _tow_trips.router,
    old_prefix="/tow-trips",
    new_prefix="/transport-trips",
    tags=["Transport Trips"],
    skip_paths=["/tow-trips/book-request"],  # overridden below to force transport
)


@transport_trips_router.post(
    "/transport-trips/book-request", response_model=TowTripSafe
)
def create_transport_booking_request(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis),
    trip_in: TowTripCreate,
):
    """Create a transport booking. Forces ``service_type="transport"`` and then
    delegates to the shared tow booking flow (pricing + role-aware dispatch)."""
    trip_in.service_type = "transport"
    return create_tow_booking_request(
        session=session,
        current_user=current_user,
        redis_client=redis_client,
        trip_in=trip_in,
    )


# ── /transport-drivers ────────────────────────────────────────────────────────
transport_drivers_router = _alias_router(
    _tow_drivers.router,
    old_prefix="/tow-truck-drivers",
    new_prefix="/transport-drivers",
    tags=["Transport Drivers"],
)
