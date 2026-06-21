"""AI Diagnostic gateway router (prefix /api/v1/ai-diagnostic).

REST surface that supports the real-time chat (see `ws.py` for the WebSocket
itself). All endpoints require the normal Core JWT (`get_current_active_user`);
the `X-Internal-Secret` to the AI service is added server-side by `ai_client` and
is never exposed to the app.

Authorization model: media is namespaced by the authenticated user id. Chat images
are uploaded via the JWT-protected multipart endpoint `/sessions/{id}/images`
(kept off the WebSocket so large bytes never sit in the chat loop); the backend
validates and stores them in R2 under the caller's own prefix and returns a `key`
the chat message then references. The user_id used for the R2 prefix and forwarded
to the AI is always taken from the token, never from the client.
`/sessions/{id}/close` purges that session's media and closes the AI row (the
WebSocket also runs this on disconnect). The old user-facing common-solution
search/browse endpoints were replaced by the chat.
"""

import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    UploadFile,
)
from app.core.rate_limit import RateLimiter

from app.core.models import User
from app.core.security import get_current_active_user
from app.modules.ai_diagnostic import ai_client, storage
from app.modules.ai_diagnostic.config import (
    AI_IMAGE_MAX_BYTES,
    AI_MAX_IMAGES_PER_SESSION,
)

router = APIRouter(prefix="/api/v1/ai-diagnostic", tags=["AI Diagnostic"])

_ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
}


def _validate_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"{field} must be a valid UUID")


@router.post(
    "/sessions/{session_id}/images",
    dependencies=[Depends(RateLimiter(times=20, seconds=60))],
)
async def upload_session_image(
    session_id: str = Path(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
):
    """Upload one chat image (multipart). The backend validates and stores it in R2
    under the caller's session prefix and returns its `key` for use in the chat.

    Kept off the WebSocket so large bytes never sit in the chat message loop. The
    per-session image cap is enforced here against the objects already stored.
    """
    session_id = _validate_uuid(session_id, "session_id")
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400, detail="file must be a JPEG/PNG/WebP/GIF/HEIC image."
        )

    user_id = str(current_user.id)
    if (
        await storage.count_session_images(user_id, session_id)
        >= AI_MAX_IMAGES_PER_SESSION
    ):
        raise HTTPException(
            status_code=409,
            detail=f"image limit reached (max {AI_MAX_IMAGES_PER_SESSION} per chat).",
        )

    raw = await file.read(AI_IMAGE_MAX_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="empty file.")
    if len(raw) > AI_IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="image too large (max 4 MB).")

    try:
        key = await storage.upload_ai_image(user_id, session_id, raw, content_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="file is not a valid image.")
    return {"key": key}


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
