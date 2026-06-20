"""AI Diagnostic gateway router (prefix /api/v1/ai-diagnostic).

All endpoints require the normal Core JWT (`get_current_active_user`). The
`X-Internal-Secret` to the AI service is added server-side by `ai_client` and is
never exposed to the app.

Authorization model: media keys are namespaced by the authenticated user id, so
every operation is scoped to the caller's own media — `session_id` is validated
as a UUID and each media `key` in /query must live under the caller's own
session prefix (blocks cross-user/arbitrary-object reads). The user_id forwarded
to the AI is always taken from the token, never from the client. Endpoints carry
a rate limit to bound abuse and AI (Gemini) cost.
"""

import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
)
from fastapi_limiter.depends import RateLimiter

from app.core.models import User
from app.core.security import get_current_active_user
from app.modules.ai_diagnostic import ai_client, storage
from app.modules.ai_diagnostic.schemas import (
    CommonSolutionsSearchRequest,
    QueryRequest,
    VehicleType,
)

router = APIRouter(prefix="/api/v1/ai-diagnostic", tags=["AI Diagnostic"])


def _is_media_type(file_type: str) -> bool:
    return file_type.startswith("image/") or file_type.startswith("video/")


def _validate_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"{field} must be a valid UUID")


@router.get(
    "/upload-credentials",
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
async def upload_credentials(
    session_id: str = Query(...),
    filename: str = Query(...),
    file_type: str = Query(...),
    current_user: User = Depends(get_current_active_user),
):
    """Mint a presigned POST so the app can upload one media object to R2."""
    if not _is_media_type(file_type):
        raise HTTPException(
            status_code=400, detail="file_type must be an image/* or video/* type."
        )
    session_id = _validate_uuid(session_id, "session_id")
    return await storage.generate_ai_upload_url(
        str(current_user.id), session_id, filename, file_type
    )


@router.post(
    "/common-solutions/search",
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
async def common_solutions_search(
    body: CommonSolutionsSearchRequest,
    current_user: User = Depends(get_current_active_user),
    x_request_id: Optional[str] = Header(default=None),
):
    """Vector search of common solutions for the given vehicle_type."""
    return await ai_client.search_common_solutions(
        body.model_dump(), request_id=x_request_id
    )


@router.get(
    "/common-solutions",
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
async def common_solutions_list(
    vehicle_type: VehicleType = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_active_user),
    x_request_id: Optional[str] = Header(default=None),
):
    """Browse all common solutions for a vehicle_type (paginated)."""
    return await ai_client.list_common_solutions(
        vehicle_type, limit, offset, request_id=x_request_id
    )


@router.post(
    "/query",
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
async def query(
    body: QueryRequest,
    current_user: User = Depends(get_current_active_user),
    x_request_id: Optional[str] = Header(default=None),
):
    """Phase B — rewrite owned media keys to presigned GET URLs and forward."""
    user_id = str(current_user.id)
    media = []
    for item in body.media:
        if not storage.is_owned_key(user_id, body.session_id, item.key):
            raise HTTPException(
                status_code=403, detail="media key does not belong to this session."
            )
        media.append(
            {"url": await storage.generate_ai_get_url(item.key), "type": item.type}
        )

    payload = {
        "user_id": user_id,
        "session_id": body.session_id,
        "vehicle_make": body.vehicle_make,
        "vehicle_model": body.vehicle_model,
        "vehicle_year": body.vehicle_year,
        "query": body.query,
        "media": media,
    }
    return await ai_client.forward_diagnose(payload, request_id=x_request_id)


@router.post(
    "/sessions/{session_id}/close",
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
async def close_session(
    session_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    x_request_id: Optional[str] = Header(default=None),
):
    """Purge this user's session media (Core, background) and close the AI row."""
    session_id = _validate_uuid(session_id, "session_id")
    background_tasks.add_task(
        storage.delete_ai_session_media, str(current_user.id), session_id
    )
    return await ai_client.close_session(session_id, request_id=x_request_id)
