"""R2 media helpers for the AI Diagnostic gateway.

Reuses the existing project R2 client (`s3_client`) from app/utils/storage.py —
this module never re-initialises credentials. Media lives in a dedicated bucket
(`AI_BUCKET_NAME`), fully isolated from the main app bucket, so KYC/profile
objects are unreachable from this feature.

Security model: every object key is namespaced by the authenticated user id —
`ai-diagnostic/sessions/{user_id}/{session_id}/{hex}{ext}`. The user_id always
comes from the JWT (never the client), and `is_owned_key` lets the router reject
any key that isn't under the caller's own prefix (prevents cross-user reads).
Uploads use a presigned POST with a `content-length-range` policy so object size
is capped server-side. boto3 is synchronous → calls are offloaded with
`asyncio.to_thread`.
"""

import os
import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import List

from botocore.exceptions import ClientError

from app.utils.storage import s3_client
from app.modules.ai_diagnostic.config import (
    AI_BUCKET_NAME,
    AI_MEDIA_PREFIX,
    AI_MEDIA_PUT_TTL,
    AI_MEDIA_GET_TTL,
    AI_MEDIA_MAX_BYTES,
)

# Extensions we accept for diagnostic media; anything else falls back to no ext.
_ALLOWED_MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".mp4",
    ".mov",
    ".webm",
    ".m4v",
}


def _safe_extension(filename: str) -> str:
    """Return a safe lowercase extension (incl. dot) or '' if not recognised."""
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in _ALLOWED_MEDIA_EXTENSIONS else ""


def session_prefix(user_id: str, session_id: str) -> str:
    """Key prefix owning one user's session media."""
    return f"{AI_MEDIA_PREFIX}{user_id}/{session_id}/"


def is_owned_key(user_id: str, session_id: str, key: str) -> bool:
    """True only if `key` lives under the caller's own session prefix."""
    return key.startswith(session_prefix(user_id, session_id))


async def generate_ai_upload_url(
    user_id: str, session_id: str, filename: str, file_type: str
) -> dict:
    """Mint a presigned POST the app uses to upload one media object.

    A presigned POST (not PUT) is used so the policy can enforce a
    `content-length-range` (size cap) and an exact `Content-Type`. The random
    object name prevents collisions/guessing; the key is namespaced by the
    authenticated user id so it can never land in another user's prefix.
    """
    ext = _safe_extension(filename)
    key = f"{session_prefix(user_id, session_id)}{secrets.token_hex(16)}{ext}"

    presigned = await asyncio.to_thread(
        s3_client.generate_presigned_post,
        Bucket=AI_BUCKET_NAME,
        Key=key,
        Fields={"Content-Type": file_type},
        Conditions=[
            {"Content-Type": file_type},
            ["content-length-range", 1, AI_MEDIA_MAX_BYTES],
        ],
        ExpiresIn=AI_MEDIA_PUT_TTL,
    )
    return {
        "upload_url": presigned["url"],
        "fields": presigned["fields"],
        "key": key,
        "max_bytes": AI_MEDIA_MAX_BYTES,
        "expires_in": AI_MEDIA_PUT_TTL,
    }


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
