"""Pydantic request models for the AI Diagnostic gateway.

Kept local to this module (not in app/core/models.py) to keep the feature
self-contained, and the single home for all request/response schemas this feature
forwards to the AI service — including the admin common-solution write models that
the admin router imports. Responses are returned verbatim from the AI service, so
no response models are declared here.

Curated solutions are scoped by `vehicle_type` (which now selects one storage
table per type): `CommonSolution{Create,Update}` for admin CRUD. `query` is
sanitized (trimmed, control-chars stripped, length-capped) before it leaves Core,
on top of the AI service's guardrail firewall. Media file names are restricted to
bare names with allowed extensions so a stored name can never inject a path
segment or absolute URL when the R2 link is composed. The real-time chat uses
`ChatStart` (opens a session for a vehicle type) and `ChatMessageIn` (one user
turn): `session_id` is validated as a UUID (it flows into R2 keys and the AI URL
path), the chat is image-only, and the media list is length-capped.
"""

import re
import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.modules.ai_diagnostic.config import AI_WS_MAX_IMAGES_PER_MESSAGE

VehicleType = Literal["car", "bike", "light_vehicle", "heavy_vehicle"]

_MEDIA_NAME_RE = re.compile(
    r"^[A-Za-z0-9._-]+\.(jpg|jpeg|png|webp|gif|mp4|mov|webm|m4v)$", re.IGNORECASE
)


def _validate_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        raise ValueError("session_id must be a valid UUID")


def _sanitize_query(value: str) -> str:
    v = "".join(ch for ch in str(value) if ch == "\n" or ch == "\t" or ch >= " ")
    v = v.strip()
    if not v:
        raise ValueError("query must not be empty")
    if len(v) > 4000:
        raise ValueError("query too long (max 4000 characters)")
    return v


def _validate_media_name(value: str) -> str:
    if (
        ".." in value
        or "/" in value
        or "\\" in value
        or not _MEDIA_NAME_RE.match(value)
    ):
        raise ValueError(
            "media file name must be a bare file name with an allowed extension"
        )
    return value


class CommonSolutionsSearchRequest(BaseModel):
    """Common-solution vector search — scoped by vehicle_type only."""

    vehicle_type: VehicleType
    query: str

    _sanitize_query = field_validator("query")(_sanitize_query)


class MediaFileIn(BaseModel):
    """One media reference stored by name (the AI service builds the R2 URL)."""

    name: str
    type: Literal["image", "video"]

    _validate_name = field_validator("name")(_validate_media_name)


class CommonSolutionCreate(BaseModel):
    """Admin create — forwarded to the AI service, which generates the embedding."""

    vehicle_type: VehicleType
    problem_title: str = Field(..., min_length=1, max_length=255)
    problem_summary: str = Field(..., min_length=1, max_length=4000)
    solution_steps: str = Field(..., min_length=1, max_length=20000)
    media_files: List[MediaFileIn] = Field(default_factory=list, max_length=50)
    keywords: List[str] = Field(default_factory=list, max_length=50)
    safety_alert: Optional[str] = Field(default=None, max_length=1000)
    is_active: bool = True


class CommonSolutionUpdate(BaseModel):
    """Admin partial update — only provided fields are changed.

    `vehicle_type` is not updatable in place (it selects the storage table); the
    admin passes it as a separate query param to locate the row.
    """

    problem_title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    problem_summary: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    solution_steps: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    media_files: Optional[List[MediaFileIn]] = Field(default=None, max_length=50)
    keywords: Optional[List[str]] = Field(default=None, max_length=50)
    safety_alert: Optional[str] = Field(default=None, max_length=1000)
    is_active: Optional[bool] = None


class VehicleComponentCreate(BaseModel):
    """Admin create for a component reference row — forwarded to the AI service,
    which validates the image reference and generates the embedding."""

    vehicle_type: VehicleType
    component_name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1, max_length=4000)
    location_guide: str = Field(..., min_length=1, max_length=4000)
    image_urls: List[str] = Field(default_factory=list, max_length=10)
    keywords: List[str] = Field(default_factory=list, max_length=50)
    is_active: bool = True


class VehicleComponentUpdate(BaseModel):
    """Admin partial update — only provided fields are changed. `vehicle_type` is a
    separate query param (it selects the storage table) and is not updatable here."""

    component_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    location_guide: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    image_urls: Optional[List[str]] = Field(default=None, max_length=10)
    keywords: Optional[List[str]] = Field(default=None, max_length=50)
    is_active: Optional[bool] = None


class ChatStart(BaseModel):
    """WS `start` frame — opens a chat scoped to a vehicle type."""

    vehicle_type: VehicleType


class ChatMessageIn(BaseModel):
    """WS `message` frame — one user turn referencing already-uploaded image keys.

    Images are uploaded out-of-band via the JWT-protected multipart endpoint, which
    returns each `key`; the chat turn only carries those keys (small strings), never
    the bytes.
    """

    session_id: str
    query: str
    image_keys: List[str] = []
    stream: bool = False  # opt-in: stream the reply as `chunk` frames then the final `assistant` frame

    _validate_session_id = field_validator("session_id")(_validate_uuid)
    _sanitize_query = field_validator("query")(_sanitize_query)

    @field_validator("image_keys")
    @classmethod
    def _cap_keys(cls, v: List[str]) -> List[str]:
        if len(v) > AI_WS_MAX_IMAGES_PER_MESSAGE:
            raise ValueError(
                f"at most {AI_WS_MAX_IMAGES_PER_MESSAGE} images per message"
            )
        return v
