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

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query

from app.core.security import get_current_admin
from app.modules.ai_diagnostic import ai_client
from app.modules.ai_diagnostic.schemas import (
    CommonSolutionCreate,
    CommonSolutionUpdate,
    VehicleType,
)

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
    x_request_id: Optional[str] = Header(default=None),
):
    """Backfill embeddings for rows inserted without one (e.g. raw SQL load)."""
    return await ai_client.admin_reindex_common_solutions(request_id=x_request_id)


@router.get("/{solution_id}")
async def get_common_solution(
    solution_id: str,
    x_request_id: Optional[str] = Header(default=None),
):
    """Fetch one common solution by id."""
    return await ai_client.admin_get_common_solution(
        solution_id, request_id=x_request_id
    )


@router.put("/{solution_id}")
async def update_common_solution(
    solution_id: str,
    body: CommonSolutionUpdate,
    x_request_id: Optional[str] = Header(default=None),
):
    """Update a common solution (re-embeds if the problem text changed)."""
    return await ai_client.admin_update_common_solution(
        solution_id, body.model_dump(exclude_unset=True), request_id=x_request_id
    )


@router.delete("/{solution_id}")
async def delete_common_solution(
    solution_id: str,
    x_request_id: Optional[str] = Header(default=None),
):
    """Delete a common solution."""
    return await ai_client.admin_delete_common_solution(
        solution_id, request_id=x_request_id
    )
