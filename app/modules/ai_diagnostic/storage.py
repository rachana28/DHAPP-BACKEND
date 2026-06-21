"""R2 media helpers for the AI Diagnostic gateway.

Reuses the existing project R2 client (`s3_client`) from app/utils/storage.py —
this module never re-initialises credentials. Media lives in a dedicated bucket
(`AI_BUCKET_NAME`), fully isolated from the main app bucket, so KYC/profile
objects are unreachable from this feature.

Security model: every object key is namespaced by the authenticated user id —
`ai-diagnostic/sessions/{user_id}/{session_id}/{hex}{ext}`. The user_id always
comes from the JWT (never the client). Chat images are uploaded through the
multipart endpoint and stored here server-side (`upload_ai_image`), so the app
never gets write credentials and the key is always minted from the JWT user id. The
bytes are sniffed by magic number so only real images are stored, regardless of the
declared content type. boto3 is synchronous → calls are offloaded with
`asyncio.to_thread`.
"""

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from botocore.exceptions import ClientError

from app.utils.storage import s3_client
from app.modules.ai_diagnostic.config import (
    AI_BUCKET_NAME,
    AI_MEDIA_PREFIX,
    AI_MEDIA_GET_TTL,
)

# Image content types accepted in the chat, mapped to a canonical extension.
_IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
}


def _sniff_image_ext(data: bytes) -> Optional[str]:
    """Return a canonical extension if `data`'s magic bytes are a known image."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] in (b"GIF8",):
        return ".gif"
    if data[4:8] == b"ftyp" and data[8:12] in (
        b"heic",
        b"heix",
        b"hevc",
        b"mif1",
        b"msf1",
    ):
        return ".heic"
    return None


def session_prefix(user_id: str, session_id: str) -> str:
    """Key prefix owning one user's session media."""
    return f"{AI_MEDIA_PREFIX}{user_id}/{session_id}/"


def is_owned_key(user_id: str, session_id: str, key: str) -> bool:
    """True only if `key` lives under the caller's own session prefix."""
    return key.startswith(session_prefix(user_id, session_id))


async def upload_ai_image(
    user_id: str, session_id: str, data: bytes, content_type: str
) -> str:
    """Store one chat image in R2 under the caller's session prefix; return its key.

    `content_type` must be an accepted image type, and the raw bytes are sniffed by
    magic number — a mismatch (or non-image payload) raises ValueError so a
    disguised file can never be stored. The object name is random and namespaced by
    the JWT user id, so it can never land in another user's prefix.
    """
    if content_type not in _IMAGE_CONTENT_TYPES:
        raise ValueError("unsupported_image_type")
    sniffed = _sniff_image_ext(data)
    if sniffed is None:
        raise ValueError("not_an_image")

    key = f"{session_prefix(user_id, session_id)}{secrets.token_hex(16)}{sniffed}"
    await asyncio.to_thread(
        s3_client.put_object,
        Bucket=AI_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


async def generate_ai_get_url(key: str) -> str:
    """Mint a short-lived presigned GET URL the AI service reads media from."""
    return await asyncio.to_thread(
        s3_client.generate_presigned_url,
        "get_object",
        Params={"Bucket": AI_BUCKET_NAME, "Key": key},
        ExpiresIn=AI_MEDIA_GET_TTL,
    )


def _delete_keys_sync(keys: List[str]) -> None:
    """Batch-delete keys from the AI bucket (up to 1000 per call)."""
    if not keys:
        return
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        try:
            s3_client.delete_objects(
                Bucket=AI_BUCKET_NAME,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except ClientError as e:
            print(f"AI R2 Delete Error: {e}")


def _list_keys_sync(prefix: str, cutoff: datetime = None) -> List[str]:
    """List object keys under `prefix`; if `cutoff` set, only older objects."""
    keys: List[str] = []
    continuation = None
    while True:
        kwargs = {"Bucket": AI_BUCKET_NAME, "Prefix": prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        try:
            resp = s3_client.list_objects_v2(**kwargs)
        except ClientError as e:
            print(f"AI R2 List Error: {e}")
            break
        for obj in resp.get("Contents", []):
            if cutoff is None:
                keys.append(obj["Key"])
            else:
                last_modified = obj.get("LastModified")
                if last_modified is not None and last_modified < cutoff:
                    keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return keys


async def count_session_images(user_id: str, session_id: str) -> int:
    """Number of media objects already stored for one user's session."""
    keys = await asyncio.to_thread(_list_keys_sync, session_prefix(user_id, session_id))
    return len(keys)


async def delete_ai_session_media(user_id: str, session_id: str) -> int:
    """Delete every media object for one user's session. Returns count deleted."""
    keys = await asyncio.to_thread(_list_keys_sync, session_prefix(user_id, session_id))
    if keys:
        await asyncio.to_thread(_delete_keys_sync, keys)
    return len(keys)


async def sweep_ai_media(max_age_hours: int = 24) -> int:
    """Delete orphaned media older than `max_age_hours`. Returns count deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    keys = await asyncio.to_thread(_list_keys_sync, AI_MEDIA_PREFIX, cutoff)
    if keys:
        await asyncio.to_thread(_delete_keys_sync, keys)
    return len(keys)
