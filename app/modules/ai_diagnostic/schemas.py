"""Pydantic request models for the AI Diagnostic gateway.

Kept local to this module (not in app/core/models.py) to keep the feature
self-contained, and the single home for all request/response schemas this feature
forwards to the AI service — including the admin common-solution write models that
the admin router imports. Responses are returned verbatim from the AI service, so
no response models are declared here.

The common-solution tier (current product) is scoped by `vehicle_type` only:
`CommonSolutionsSearchRequest` for vector search and `CommonSolution{Create,Update}`
for admin CRUD. `query` is sanitized (trimmed, control-chars stripped, length-capped)
before it leaves Core, on top of the AI service's guardrail firewall. Media file
names are restricted to bare names with allowed extensions so a stored name can
never inject a path segment or absolute URL when the R2 link is composed.
`QueryRequest`/`MediaItemIn` remain for the dormant AI chat: `session_id` is
validated as a UUID (it flows into R2 keys and the AI URL path) and the media list
is length-capped.
"""

import re
import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.modules.ai_diagnostic.config import AI_MEDIA_MAX_ITEMS

VehicleType = Literal["car", "bike", "light_vehicle", "heavy_vehicle"]

_MEDIA_NAME_RE = re.compile(
    r"^[A-Za-z0-9._-]+\.(jpg|jpeg|png|webp|gif|mp4|mov|webm|m4v)$", re.IGNORECASE
)


def _validate_year(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    v = str(value).strip()
    if not (len(v) == 4 and v.isdigit()):
        raise ValueError("vehicle_year must be 4 digits")
    return v


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
    """Admin partial update — only provided fields are changed."""

    vehicle_type: Optional[VehicleType] = None
    problem_title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    problem_summary: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    solution_steps: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    media_files: Optional[List[MediaFileIn]] = Field(default=None, max_length=50)
    keywords: Optional[List[str]] = Field(default=None, max_length=50)
    safety_alert: Optional[str] = Field(default=None, max_length=1000)
    is_active: Optional[bool] = None


class MediaItemIn(BaseModel):
    """One uploaded media object referenced by its R2 key."""

    key: str
    type: Literal["image", "video"]


class QueryRequest(BaseModel):
    """Phase B — AI chat request. The app sends R2 keys, not URLs."""

    session_id: str
    vehicle_make: str
    vehicle_model: str
    vehicle_year: Optional[str] = None
    query: str
    media: List[MediaItemIn] = []

    _validate_vehicle_year = field_validator("vehicle_year")(_validate_year)
    _validate_session_id = field_validator("session_id")(_validate_uuid)

    @field_validator("media")
    @classmethod
    def _cap_media(cls, v: List[MediaItemIn]) -> List[MediaItemIn]:
        if len(v) > AI_MEDIA_MAX_ITEMS:
            raise ValueError(f"media may contain at most {AI_MEDIA_MAX_ITEMS} items")
        return v
