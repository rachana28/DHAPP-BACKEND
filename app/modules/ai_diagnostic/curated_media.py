"""Admin upload/delete of curated media (common-solutions & vehicle-components).

Lets an admin store the actual image/video for a curated entry, named exactly as the
`media_files` / `image_urls` value the AI service stores, so the public URL the AI
service builds resolves to the uploaded object. Reuses the project R2 client
(`app/utils/storage.py:s3_client`) and the image magic-byte sniffer from this module's
`storage.py`; videos are sniffed here. The object is written to the AI diagnostic R2
bucket (AI_BUCKET_NAME) at `<prefix>/<vehicle_type>/<name>`, where the prefix matches the
AI service's COMMON_MEDIA_PREFIX / COMPONENT_MEDIA_PREFIX.

Security: the file name must be a bare name with an allowed extension (no path
segments), the size must be within the per-category cap, and the raw bytes are sniffed
by magic number — a payload whose real type does not match its declared extension
category (e.g. a script renamed to .jpg) is rejected. boto3 is synchronous, so calls
are offloaded with `asyncio.to_thread`.
"""

import asyncio
import re
from typing import Optional

from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.modules.ai_diagnostic.config import (
    AI_BUCKET_NAME,
    CURATED_IMAGE_MAX_BYTES,
    CURATED_MEDIA_PUBLIC_URL,
    CURATED_VIDEO_MAX_BYTES,
)
from app.modules.ai_diagnostic.storage import _sniff_image_ext
from app.utils.storage import s3_client

_TARGET_PREFIX = {
    "common_solutions": "common-solutions",
    "vehicle_components": "vehicle-components",
}
_VEHICLE_TYPES = {"car", "bike", "light_vehicle", "heavy_vehicle"}
_NAME_RE = re.compile(
    r"^[A-Za-z0-9._-]+\.(jpg|jpeg|png|webp|gif|mp4|mov|webm|m4v)$", re.IGNORECASE
)
_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
_VIDEO_EXT = {"mp4", "mov", "webm", "m4v"}
_CONTENT_TYPE = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "m4v": "video/x-m4v",
}


def _sniff_video(data: bytes) -> bool:
    if data[4:8] == b"ftyp":  # mp4 / m4v / mov ISO base media
        return True
    if data[:4] == b"\x1a\x45\xdf\xa3":  # EBML container (webm/mkv)
        return True
    return False


def _bytes_category(data: bytes) -> Optional[str]:
    if _sniff_image_ext(data) is not None:
        return "image"
    if _sniff_video(data):
        return "video"
    return None


def _validate(target: str, vehicle_type: str, name: str) -> tuple[str, str]:
    prefix = _TARGET_PREFIX.get(target)
    if prefix is None:
        raise HTTPException(status_code=400, detail="Invalid target.")
    if vehicle_type not in _VEHICLE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid vehicle_type.")
    name = (name or "").strip()
    if ".." in name or "/" in name or "\\" in name or not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid media file name.")
    return prefix, name


async def upload_curated_media(
    target: str,
    vehicle_type: str,
    name: str,
    data: bytes,
    content_type: Optional[str] = None,
) -> dict:
    prefix, name = _validate(target, vehicle_type, name)
    ext = name.rsplit(".", 1)[-1].lower()
    name_category = "image" if ext in _IMAGE_EXT else "video"

    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    cap = (
        CURATED_IMAGE_MAX_BYTES if name_category == "image" else CURATED_VIDEO_MAX_BYTES
    )
    if len(data) > cap:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {cap // (1024 * 1024)} MB for {name_category}).",
        )
    if _bytes_category(data) != name_category:
        raise HTTPException(
            status_code=400, detail="File content does not match its declared type."
        )

    key = f"{prefix}/{vehicle_type}/{name}"
    try:
        await asyncio.to_thread(
            s3_client.put_object,
            Bucket=AI_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=_CONTENT_TYPE.get(
                ext, content_type or "application/octet-stream"
            ),
        )
    except ClientError as exc:
        raise HTTPException(status_code=502, detail="Failed to store media.") from exc

    out = {"name": name, "key": key}
    if CURATED_MEDIA_PUBLIC_URL:
        out["url"] = f"{CURATED_MEDIA_PUBLIC_URL.rstrip('/')}/{key}"
    return out


async def delete_curated_media(target: str, vehicle_type: str, name: str) -> dict:
    prefix, name = _validate(target, vehicle_type, name)
    key = f"{prefix}/{vehicle_type}/{name}"
    try:
        await asyncio.to_thread(s3_client.delete_object, Bucket=AI_BUCKET_NAME, Key=key)
    except ClientError as exc:
        raise HTTPException(status_code=502, detail="Failed to delete media.") from exc
    return {"name": name, "key": key, "deleted": True}
