"""Configuration for the AI Diagnostic gateway.

Reads env vars at import time (the project loads `.env` once via `load_dotenv()`
in app/core/database.py). All diagnostic media lives under a single R2 key
prefix so per-session purge and the 24h orphan sweep can target it cheaply.
"""

import os

# Private URL of the AI Diagnostic service (e.g. Render internal network).
AI_DIAGNOSTIC_BASE_URL = os.getenv("AI_DIAGNOSTIC_BASE_URL")

# Shared secret sent as `X-Internal-Secret`; must equal the AI service's
# EXPECTED_INTERNAL_SECRET (>= 16 chars).
INTERNAL_SECRET = os.getenv("INTERNAL_SECRET")

# Second shared secret sent as `X-Admin-Secret` only on admin common-solution
# calls; must equal the AI service's EXPECTED_ADMIN_SECRET (>= 16 chars). Never
# attached to user-facing requests.
AI_ADMIN_SECRET = os.getenv("AI_ADMIN_SECRET")

# Presigned upload (PUT) lifetime handed to the app, seconds.
AI_MEDIA_PUT_TTL = int(os.getenv("AI_MEDIA_PUT_TTL", "900"))

# Presigned read (GET) lifetime handed to the AI service, seconds.
AI_MEDIA_GET_TTL = int(os.getenv("AI_MEDIA_GET_TTL", "300"))

# Dedicated R2 bucket for AI diagnostic media (isolated from the main app
# bucket so KYC/profile objects are never reachable from this feature). Uses the
# same R2 credentials/endpoint (shared boto3 client), only a different bucket.
AI_BUCKET_NAME = os.getenv("AI_DIAGNOSTIC_BUCKET_NAME", "dhire-ai-diagnostic")

# Key prefix under which every session's media objects are stored. Layout is
# `ai-diagnostic/sessions/{user_id}/{session_id}/...` so a caller can only ever
# reach their own media (ownership is derived from the JWT, never the client).
AI_MEDIA_PREFIX = "ai-diagnostic/sessions/"

# Hard cap on a single uploaded object (enforced in the presigned POST policy).
AI_MEDIA_MAX_BYTES = int(os.getenv("AI_MEDIA_MAX_BYTES", str(50 * 1024 * 1024)))

# Max images allowed across a whole chat session (cost/quota guard, mirrors the
# AI service's MAX_IMAGES_PER_SESSION for a fast client-facing reject).
AI_MAX_IMAGES_PER_SESSION = int(os.getenv("AI_MAX_IMAGES_PER_SESSION", "3"))

# Chat images are uploaded via a separate JWT-protected multipart endpoint (kept
# off the WebSocket to bound memory); the backend streams them to R2 and the chat
# references the returned key. Per-image byte cap and per-message key count.
AI_IMAGE_MAX_BYTES = int(os.getenv("AI_IMAGE_MAX_BYTES", str(4 * 1024 * 1024)))
AI_WS_MAX_IMAGES_PER_MESSAGE = int(os.getenv("AI_WS_MAX_IMAGES_PER_MESSAGE", "2"))

# Curated media (admin-uploaded images/videos for common-solutions & vehicle-components).
CURATED_MEDIA_PUBLIC_URL = os.getenv("CURATED_MEDIA_PUBLIC_URL")  # optional, for preview URLs
CURATED_IMAGE_MAX_BYTES = int(os.getenv("CURATED_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))
CURATED_VIDEO_MAX_BYTES = int(os.getenv("CURATED_VIDEO_MAX_BYTES", str(25 * 1024 * 1024)))
