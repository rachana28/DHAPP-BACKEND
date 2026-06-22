"""Async httpx wrapper over the external AI Diagnostic service.

Holds one module-level `httpx.AsyncClient` (connection pooling) configured with
the AI base URL and a generous read timeout (`/diagnose` may call Gemini). Every
call attaches `X-Internal-Secret` (the shared secret, never exposed to the app)
and a correlation `X-Request-ID`; the `admin_*` common-solution calls additionally
attach `X-Admin-Secret` (the second secret, only ever sent by the admin router).
Errors are mapped to consistent HTTP statuses so callers/clients see predictable
failures. `aclose()` is called from the app lifespan shutdown.
"""

import json
import logging
import uuid
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.modules.ai_diagnostic.config import (
    AI_ADMIN_SECRET,
    AI_DIAGNOSTIC_BASE_URL,
    INTERNAL_SECRET,
)

logger = logging.getLogger("ai_diagnostic.client")

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

    content_type = resp.headers.get("content-type", "")
    is_json = "application/json" in content_type.lower()

    if resp.is_success:
        if not is_json:
            logger.error(
                "AI service returned non-JSON success [%s %s] status=%s ct=%s body=%.200s",
                method,
                path,
                resp.status_code,
                content_type,
                resp.text,
            )
            raise HTTPException(
                status_code=502,
                detail="AI diagnostic service returned an invalid response.",
            )
        try:
            return resp.json()
        except ValueError:
            logger.error(
                "AI service JSON parse failed [%s %s] body=%.200s",
                method,
                path,
                resp.text,
            )
            raise HTTPException(
                status_code=502,
                detail="AI diagnostic service returned an invalid response.",
            )

    # Non-2xx: only surface a clean JSON `detail`; never forward an HTML/error page.
    detail: Any = f"AI diagnostic service error ({resp.status_code})."
    if is_json:
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(
                body.get("detail"), (str, list, dict)
            ):
                detail = body["detail"]
        except ValueError:
            pass
    else:
        logger.error(
            "AI service non-2xx non-JSON [%s %s] status=%s ct=%s body=%.300s",
            method,
            path,
            resp.status_code,
            content_type,
            resp.text,
        )
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
    solution_id: str, vehicle_type: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "GET",
        f"{_ADMIN_BASE}/{quote(solution_id, safe='')}",
        request_id,
        params={"vehicle_type": vehicle_type},
        admin=True,
    )


async def admin_update_common_solution(
    solution_id: str, vehicle_type: str, payload: dict, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "PUT",
        f"{_ADMIN_BASE}/{quote(solution_id, safe='')}",
        request_id,
        params={"vehicle_type": vehicle_type},
        json=payload,
        admin=True,
    )


async def admin_delete_common_solution(
    solution_id: str, vehicle_type: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "DELETE",
        f"{_ADMIN_BASE}/{quote(solution_id, safe='')}",
        request_id,
        params={"vehicle_type": vehicle_type},
        admin=True,
    )


async def admin_reindex_common_solutions(
    vehicle_type: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "POST",
        f"{_ADMIN_BASE}/reindex",
        request_id,
        json={"vehicle_type": vehicle_type},
        admin=True,
    )


async def admin_reindex_status_common_solutions(
    ref_id: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "GET",
        f"{_ADMIN_BASE}/reindex/status",
        request_id,
        params={"ref_id": ref_id},
        admin=True,
    )


# --- Vehicle components: admin surface (attaches X-Admin-Secret) ---

_COMPONENT_ADMIN_BASE = "/api/v1/admin/vehicle-components"


async def admin_create_component(
    payload: dict, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "POST", _COMPONENT_ADMIN_BASE, request_id, json=payload, admin=True
    )


async def admin_list_components(
    vehicle_type: Optional[str],
    limit: int,
    offset: int,
    request_id: Optional[str] = None,
) -> Any:
    params: dict = {"limit": limit, "offset": offset}
    if vehicle_type:
        params["vehicle_type"] = vehicle_type
    return await _request(
        "GET", _COMPONENT_ADMIN_BASE, request_id, params=params, admin=True
    )


async def admin_get_component(
    component_id: str, vehicle_type: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "GET",
        f"{_COMPONENT_ADMIN_BASE}/{quote(component_id, safe='')}",
        request_id,
        params={"vehicle_type": vehicle_type},
        admin=True,
    )


async def admin_update_component(
    component_id: str,
    vehicle_type: str,
    payload: dict,
    request_id: Optional[str] = None,
) -> Any:
    return await _request(
        "PUT",
        f"{_COMPONENT_ADMIN_BASE}/{quote(component_id, safe='')}",
        request_id,
        params={"vehicle_type": vehicle_type},
        json=payload,
        admin=True,
    )


async def admin_delete_component(
    component_id: str, vehicle_type: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "DELETE",
        f"{_COMPONENT_ADMIN_BASE}/{quote(component_id, safe='')}",
        request_id,
        params={"vehicle_type": vehicle_type},
        admin=True,
    )


async def admin_reindex_components(
    vehicle_type: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "POST",
        f"{_COMPONENT_ADMIN_BASE}/reindex",
        request_id,
        json={"vehicle_type": vehicle_type},
        admin=True,
    )


async def admin_reindex_status_components(
    ref_id: str, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "GET",
        f"{_COMPONENT_ADMIN_BASE}/reindex/status",
        request_id,
        params={"ref_id": ref_id},
        admin=True,
    )


_UNRESOLVED_BASE = "/api/v1/admin/unresolved-queries"


async def admin_list_unresolved(
    status: Optional[str],
    limit: int,
    offset: int,
    request_id: Optional[str] = None,
) -> Any:
    params: dict = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    return await _request(
        "GET", _UNRESOLVED_BASE, request_id, params=params, admin=True
    )


async def admin_update_unresolved_status(
    query_id: str, payload: dict, request_id: Optional[str] = None
) -> Any:
    return await _request(
        "PATCH",
        f"{_UNRESOLVED_BASE}/{quote(query_id, safe='')}",
        request_id,
        json=payload,
        admin=True,
    )


async def forward_diagnose(payload: dict, request_id: Optional[str] = None) -> Any:
    """Real-time chat turn: POST /api/v1/diagnose."""
    return await _post("/api/v1/diagnose", payload, request_id)


async def forward_diagnose_stream(
    payload: dict, request_id: Optional[str] = None
) -> AsyncIterator[dict]:
    """Stream a chat turn (SSE) from the AI service: POST /api/v1/diagnose/stream.

    Yields each parsed event dict (`token` / `final` / `error` / `done`). Connection
    and non-2xx errors are mapped to HTTPException and raised before the first event,
    mirroring `_request`; the caller relays errors to the client as a WS frame.
    """
    headers = _headers(request_id)
    headers["Accept"] = "text/event-stream"
    try:
        async with _client.stream(
            "POST", "/api/v1/diagnose/stream", json=payload, headers=headers
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                detail: Any = f"AI diagnostic service error ({resp.status_code})."
                try:
                    parsed = json.loads(body.decode("utf-8", "ignore"))
                    if isinstance(parsed, dict) and parsed.get("detail"):
                        detail = parsed["detail"]
                except ValueError:
                    pass
                raise HTTPException(status_code=resp.status_code, detail=detail)

            async for line in resp.aiter_lines():
                if not line:
                    continue
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                data = stripped[len("data:") :].strip()
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except ValueError:
                    continue
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI diagnostic service timed out.")
    except httpx.TransportError:
        raise HTTPException(
            status_code=502, detail="Could not reach AI diagnostic service."
        )


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
