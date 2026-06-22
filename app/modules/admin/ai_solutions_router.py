"""Admin common-solution management (prefix /admin/ai-diagnostic/common-solutions).

Thin gateway from the admin dashboard to the AI Diagnostic service's admin CRUD.
The whole router is admin-only via the router-level `get_current_admin` dependency
(same gate as the rest of the admin module); `ai_client` then attaches both the
`X-Internal-Secret` and the admin-only `X-Admin-Secret` so the AI service applies
its own second gate. Request bodies are validated with the shared schemas that
live in the `ai_diagnostic` module (single source of truth), which also enforce
the vehicle_type enum and safe media file names before anything is forwarded.

Additive only — no existing admin logic is touched.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, UploadFile

from app.core.security import get_current_admin
from app.modules.ai_diagnostic import ai_client, curated_media
from app.modules.ai_diagnostic.config import CURATED_VIDEO_MAX_BYTES
from app.modules.ai_diagnostic.schemas import (
    CommonSolutionCreate,
    CommonSolutionUpdate,
    VehicleComponentCreate,
    VehicleComponentUpdate,
    VehicleType,
)

UnresolvedStatus = Literal["open", "in_progress", "resolved", "dismissed"]

router = APIRouter(
    prefix="/admin/ai-diagnostic/common-solutions",
    tags=["Admin AI Diagnostic"],
    dependencies=[Depends(get_current_admin)],
)


@router.post("")
async def create_common_solution(
    body: CommonSolutionCreate,
    x_request_id: Optional[str] = Header(default=None),
):
    """Create a common solution (the AI service generates its embedding)."""
    return await ai_client.admin_create_common_solution(
        body.model_dump(), request_id=x_request_id
    )


@router.get("")
async def list_common_solutions(
    vehicle_type: Optional[VehicleType] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    x_request_id: Optional[str] = Header(default=None),
):
    """List common solutions (all states), optionally filtered by vehicle_type."""
    return await ai_client.admin_list_common_solutions(
        vehicle_type, limit, offset, request_id=x_request_id
    )


@router.post("/reindex")
async def reindex_common_solutions(
    vehicle_type: VehicleType = Body(..., embed=True),
    x_request_id: Optional[str] = Header(default=None),
):
    """Start a background embedding backfill for one vehicle_type's table.

    Returns a `ref_id`; poll GET /reindex/status?ref_id=... for progress. Safe to
    call repeatedly — it resumes where it left off and returns the in-flight job's
    ref_id with status 'already_running' if one is running for that type.
    """
    return await ai_client.admin_reindex_common_solutions(
        vehicle_type, request_id=x_request_id
    )


@router.get("/reindex/status")
async def reindex_status_common_solutions(
    ref_id: str = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Progress of a reindex job by ref_id (running, pending, processed, failed)."""
    return await ai_client.admin_reindex_status_common_solutions(
        ref_id, request_id=x_request_id
    )


@router.get("/{solution_id}")
async def get_common_solution(
    solution_id: str,
    vehicle_type: VehicleType = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Fetch one common solution by id (vehicle_type selects its table)."""
    return await ai_client.admin_get_common_solution(
        solution_id, vehicle_type, request_id=x_request_id
    )


@router.put("/{solution_id}")
async def update_common_solution(
    solution_id: str,
    body: CommonSolutionUpdate,
    vehicle_type: VehicleType = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Update a common solution (re-embeds if the problem text changed)."""
    return await ai_client.admin_update_common_solution(
        solution_id,
        vehicle_type,
        body.model_dump(exclude_unset=True),
        request_id=x_request_id,
    )


@router.delete("/{solution_id}")
async def delete_common_solution(
    solution_id: str,
    vehicle_type: VehicleType = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Delete a common solution (vehicle_type selects its table)."""
    return await ai_client.admin_delete_common_solution(
        solution_id, vehicle_type, request_id=x_request_id
    )


component_router = APIRouter(
    prefix="/admin/ai-diagnostic/vehicle-components",
    tags=["Admin AI Diagnostic"],
    dependencies=[Depends(get_current_admin)],
)


@component_router.post("")
async def create_component(
    body: VehicleComponentCreate,
    x_request_id: Optional[str] = Header(default=None),
):
    """Create a component reference row (the AI service generates its embedding)."""
    return await ai_client.admin_create_component(
        body.model_dump(), request_id=x_request_id
    )


@component_router.get("")
async def list_components(
    vehicle_type: Optional[VehicleType] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    x_request_id: Optional[str] = Header(default=None),
):
    """List component references (all states), optionally filtered by vehicle_type."""
    return await ai_client.admin_list_components(
        vehicle_type, limit, offset, request_id=x_request_id
    )


@component_router.post("/reindex")
async def reindex_components(
    vehicle_type: VehicleType = Body(..., embed=True),
    x_request_id: Optional[str] = Header(default=None),
):
    """Start a background embedding backfill for one vehicle_type's component table."""
    return await ai_client.admin_reindex_components(
        vehicle_type, request_id=x_request_id
    )


@component_router.get("/reindex/status")
async def reindex_status_components(
    ref_id: str = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Progress of a component reindex job by ref_id."""
    return await ai_client.admin_reindex_status_components(
        ref_id, request_id=x_request_id
    )


@component_router.get("/{component_id}")
async def get_component(
    component_id: str,
    vehicle_type: VehicleType = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Fetch one component reference by id (vehicle_type selects its table)."""
    return await ai_client.admin_get_component(
        component_id, vehicle_type, request_id=x_request_id
    )


@component_router.put("/{component_id}")
async def update_component(
    component_id: str,
    body: VehicleComponentUpdate,
    vehicle_type: VehicleType = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Update a component reference (re-embeds if name/description/location changed)."""
    return await ai_client.admin_update_component(
        component_id,
        vehicle_type,
        body.model_dump(exclude_unset=True),
        request_id=x_request_id,
    )


@component_router.delete("/{component_id}")
async def delete_component(
    component_id: str,
    vehicle_type: VehicleType = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Delete a component reference (vehicle_type selects its table)."""
    return await ai_client.admin_delete_component(
        component_id, vehicle_type, request_id=x_request_id
    )


media_router = APIRouter(
    prefix="/admin/ai-diagnostic/media",
    tags=["Admin AI Diagnostic"],
    dependencies=[Depends(get_current_admin)],
)


@media_router.post("")
async def upload_curated_media(
    target: str = Form(...),
    vehicle_type: VehicleType = Form(...),
    name: str = Form(...),
    file: UploadFile = File(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Upload one curated image/video to R2 under `<prefix>/<vehicle_type>/<name>`.

    `target` is 'common_solutions' or 'vehicle_components'; `name` is the exact file
    name that will be stored in the entry's media list. Format/size are validated
    (incl. magic-byte sniffing) before the object is written.
    """
    data = await file.read(CURATED_VIDEO_MAX_BYTES + 1)
    return await curated_media.upload_curated_media(
        target, vehicle_type, name, data, file.content_type
    )


@media_router.delete("")
async def delete_curated_media(
    target: str = Query(...),
    vehicle_type: VehicleType = Query(...),
    name: str = Query(...),
    x_request_id: Optional[str] = Header(default=None),
):
    """Delete one curated media object (used when replacing/removing media)."""
    return await curated_media.delete_curated_media(target, vehicle_type, name)


unresolved_router = APIRouter(
    prefix="/admin/ai-diagnostic/unresolved-queries",
    tags=["Admin AI Diagnostic"],
    dependencies=[Depends(get_current_admin)],
)


@unresolved_router.get("")
async def list_unresolved_queries(
    status: Optional[UnresolvedStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    x_request_id: Optional[str] = Header(default=None),
):
    """List queries the AI could not resolve, optionally filtered by status."""
    return await ai_client.admin_list_unresolved(
        status, limit, offset, request_id=x_request_id
    )


@unresolved_router.patch("/{query_id}")
async def update_unresolved_query(
    query_id: str,
    status: UnresolvedStatus = Body(..., embed=True),
    admin_notes: Optional[str] = Body(default=None, embed=True),
    x_request_id: Optional[str] = Header(default=None),
):
    """Update the status / research notes on an unresolved query."""
    payload = {"status": status, "admin_notes": admin_notes}
    return await ai_client.admin_update_unresolved_status(
        query_id, payload, request_id=x_request_id
    )
