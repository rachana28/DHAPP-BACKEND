"""Async httpx wrapper over the external AI Diagnostic service.

Holds one module-level `httpx.AsyncClient` (connection pooling) configured with
the AI base URL and a generous read timeout (`/diagnose` may call Gemini). Every
call attaches `X-Internal-Secret` (the shared secret, never exposed to the app)
and a correlation `X-Request-ID`; the `admin_*` common-solution calls additionally
attach `X-Admin-Secret` (the second secret, only ever sent by the admin router).
Errors are mapped to consistent HTTP statuses so callers/clients see predictable
failures. `aclose()` is called from the app lifespan shutdown.
"""

import uuid
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.modules.ai_diagnostic.config import (
    AI_ADMIN_SECRET,
    AI_DIAGNOSTIC_BASE_URL,
    INTERNAL_SECRET,
)

_client = httpx.AsyncClient(
    base_url=AI_DIAGNOSTIC_BASE_URL or "",
    timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=60.0),
)


def _safe_request_id(request_id: Optional[str]) -> str:
    """Use the inbound correlation id only if it's a valid UUID, else mint one.

    Prevents header/log injection from an attacker-controlled X-Request-ID.
    """
    if request_id:
        try:
            return str(uuid.UUID(request_id))
        except (ValueError, AttributeError):
            pass
    return str(uuid.uuid4())


def _headers(request_id: Optional[str], admin: bool = False) -> dict:
    headers = {
        "X-Internal-Secret": INTERNAL_SECRET or "",
        "X-Request-ID": _safe_request_id(request_id),
    }
    if admin:
        headers["X-Admin-Secret"] = AI_ADMIN_SECRET or ""
    return headers


async def _request(
    method: str,
    path: str,
    request_id: Optional[str],
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    admin: bool = False,
) -> Any:
    """Send a request to the AI service and return parsed JSON, mapping errors.

    - read/connect timeout -> 504
    - non-2xx from the AI -> pass the upstream status through (surface `detail`)
    - connection/transport error -> 502
    """
    try:
        resp = await _client.request(
            method, path, params=params, json=json, headers=_headers(request_id, admin)
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI diagnostic service timed out.")
    except httpx.TransportError:
        raise HTTPException(
            status_code=502, detail="Could not reach AI diagnostic service."
        )

    if resp.is_success:
        return resp.json()

    # Surface the upstream status and detail where present.
    detail: Any = f"AI diagnostic service error ({resp.status_code})."
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail") is not None:
            detail = body["detail"]
    except ValueError:
        if resp.text:
            detail = resp.text
    raise HTTPException(status_code=resp.status_code, detail=detail)


async def _post(path: str, payload: Optional[dict], request_id: Optional[str]) -> Any:
    """POST to the AI service (user surface)."""
    return await _request("POST", path, request_id, json=payload)


# --- Common solutions: user surface ---


async def search_common_solutions(
    payload: dict, request_id: Optional[str] = None
) -> Any:
    """Vector search: POST /api/v1/common-solutions/search."""
    return await _post("/api/v1/common-solutions/search", payload, request_id)


async def list_common_solutions(
    vehicle_type: str,
    limit: int,
    offset: int,
    request_id: Optional[str] = None,
) -> Any:
    """Browse: GET /api/v1/common-solutions?vehicle_type=&limit=&offset=."""
    return await _request(
        "GET",
        "/api/v1/common-solutions",
        request_id,
        params={"vehicle_type": vehicle_type, "limit": limit, "offset": offset},
    )


# --- Common solutions: admin surface (attaches X-Admin-Secret) ---

_ADMIN_BASE = "/api/v1/admin/common-solutions"


async def admin_create_common_solution(
    payload: dict, request_id: Optional[str] = None
) -> Any:
    return await _request("POST", _ADMIN_BASE, request_id, json=payload, admin=True)


async def admin_list_common_solutions(
    vehicle_type: Optional[str],
    limit: int,
    offset: int,
    request_id: Optional[str] = None,
) -> Any:
    params: dict = {"limit": limit, "offset": offset}
    if vehicle_type:
        params["vehicle_type"] = vehicle_type
    return await _request("GET", _ADMIN_BASE, request_id, params=params, admin=True)


async def admin_get_common_solution(
    solution_id: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "GET", f"{_ADMIN_BASE}/{quote(solution_id, safe='')}", request_id, admin=True
    )


async def admin_update_common_solution(
    solution_id: str, payload: dict, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "PUT",
        f"{_ADMIN_BASE}/{quote(solution_id, safe='')}",
        request_id,
        json=payload,
        admin=True,
    )


async def admin_delete_common_solution(
    solution_id: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "DELETE", f"{_ADMIN_BASE}/{quote(solution_id, safe='')}", request_id, admin=True
    )


async def admin_reindex_common_solutions(request_id: Optional[str] = None) -> Any:
    return await _request(
        "POST", f"{_ADMIN_BASE}/reindex", request_id, json=None, admin=True
    )


async def admin_reindex_status_common_solutions(request_id: Optional[str] = None) -> Any:
    return await _request(
        "GET", f"{_ADMIN_BASE}/reindex/status", request_id, admin=True
    )


async def forward_diagnose(payload: dict, request_id: Optional[str] = None) -> Any:
    """Phase B — AI chat: POST /api/v1/diagnose."""
    return await _post("/api/v1/diagnose", payload, request_id)


async def close_session(session_id: str, request_id: Optional[str] = None) -> Any:
    """Delete the AI session row: POST /api/v1/sessions/{id}/close.

    `session_id` is URL-encoded (`safe=""`) so it cannot inject `/` or `..` path
    segments and redirect the call to another AI endpoint.
    """
    return await _post(
        f"/api/v1/sessions/{quote(session_id, safe='')}/close", None, request_id
    )


async def trigger_orphan_sweep(request_id: Optional[str] = None) -> Any:
    """Safety-net row sweep: POST /api/v1/internal/sweep-orphans."""
    return await _post("/api/v1/internal/sweep-orphans", None, request_id)


async def aclose() -> None:
    """Close the shared httpx client (called on app shutdown)."""
    await _client.aclose()
