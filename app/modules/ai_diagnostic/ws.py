"""Real-time AI Diagnostic chat WebSocket (path /api/v1/ai-diagnostic/ws).

The mobile app opens one socket per chat, authenticated by the normal Core JWT
passed as the `?token=` query param (same scheme as the support chat). Core owns
auth, R2 media, rate limiting and the per-user connection cap; each user turn is
forwarded to the internal AI service over HTTP (`ai_client.forward_diagnose`),
which runs the RAG/expert-mechanic logic and returns one reply. The AI service is
never exposed to the app and never sees the JWT.

Protocol
  client -> server:
    {"type":"start","vehicle_type":"car"}            open a session for a type
    {"type":"message","session_id":..,"query":..,"image_keys":["<r2-key>"],"stream":false}
    {"type":"typing"}                                 lightweight, ignored
    {"type":"close"}                                  end + purge
  server -> client:
    {"type":"session","session_id":..}
    {"type":"need_vehicle_type"}                      message before start
    {"type":"chunk","delta":..}                       streamed token (only if stream:true)
    {"type":"assistant","kind":"answer|clarify|fallback","markdown":..,"safety_alert":..,"images":[{"component_name":..,"location_guide":..,"image_urls":[..]}]}
    {"type":"image_limit"}                            per-session image cap hit
    {"type":"error","detail":..}

Streaming is OPT-IN: set `"stream": true` on a `message` to receive incremental
`chunk` frames followed by the same final `assistant` frame (the complete answer).
Omit it and the turn behaves exactly as before (one `assistant` frame, no chunks).

Vehicle type is mandatory before any diagnosis (it selects the AI service's vector
table). Session id is minted by Core on `start`. Images are uploaded out-of-band
via POST /sessions/{id}/images (see router.py) which returns a `key`; the `message`
frame only carries those keys, so image bytes never enter the WebSocket loop. Core
validates each key is owned, mints short-lived presigned GET URLs and forwards them
to the AI service. On close/disconnect the session's R2 media is deleted and the AI
session row is closed (which clears its chat history).
"""

import json
import time
import uuid
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.models import User
from app.core.security import SECRET_KEY, ALGORITHM
from app.modules.ai_diagnostic import ai_client, storage
from app.modules.ai_diagnostic.config import AI_MAX_IMAGES_PER_SESSION
from app.modules.ai_diagnostic.schemas import ChatMessageIn, ChatStart
from app.modules.ai_diagnostic.ws_manager import manager

router = APIRouter(prefix="/api/v1/ai-diagnostic", tags=["AI Diagnostic (WS)"])

RATE_LIMIT_MESSAGES = 10
RATE_LIMIT_WINDOW_SECONDS = 10
# Only small JSON frames travel here (image bytes go via the upload endpoint).
MAX_FRAME_BYTES = 64 * 1024


def _resolve_user_from_token(
    token: str, session: Session
) -> Optional[Tuple[User, str]]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    sub_val = payload.get("sub")
    role = payload.get("role")
    if not sub_val or not role:
        return None
    if role == "admin":
        user = session.exec(
            select(User).where(User.email == sub_val, User.role == role)
        ).first()
    else:
        user = session.exec(
            select(User).where(User.phone_number == sub_val, User.role == role)
        ).first()
    if not user:
        return None
    return user, role


async def _send(ws: WebSocket, payload: dict) -> None:
    await ws.send_text(json.dumps(payload, default=str))


async def _relay_diagnose_stream(ws: WebSocket, payload: dict) -> bool:
    """Relay an SSE diagnose stream as `chunk` frames + a final `assistant` frame.

    Returns True only if a final answer was delivered (so the caller can charge the
    image quota exactly as the non-streaming path does). Errors are mapped to the
    same client frames the non-streaming path sends.
    """
    got_final = False
    try:
        async for event in ai_client.forward_diagnose_stream(payload):
            etype = event.get("event")
            if etype == "token":
                delta = event.get("delta") or ""
                if delta:
                    await _send(ws, {"type": "chunk", "delta": delta})
            elif etype == "final":
                got_final = True
                await _send(
                    ws,
                    {
                        "type": "assistant",
                        "kind": event.get("kind"),
                        "markdown": event.get("answer_markdown"),
                        "safety_alert": event.get("safety_alert"),
                        "images": event.get("images") or [],
                    },
                )
            elif etype == "error":
                await _send(
                    ws, {"type": "error", "detail": event.get("detail", "ai_error")}
                )
        return got_final
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", "ai_unavailable")
        if isinstance(detail, str) and "IMAGE_LIMIT_REACHED" in detail:
            await _send(ws, {"type": "image_limit"})
        else:
            await _send(ws, {"type": "error", "detail": str(detail)})
        return False


@router.websocket("/ws")
async def ai_diagnostic_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
    session: Session = Depends(get_session),
):
    if not token:
        await websocket.close(code=4401)
        return
    resolved = _resolve_user_from_token(token, session)
    if not resolved:
        await websocket.close(code=4401)
        return
    current_user, _role = resolved
    user_id = str(current_user.id)

    await websocket.accept()
    if not await manager.connect(user_id, websocket):
        await _send(websocket, {"type": "error", "detail": "too_many_connections"})
        await websocket.close(code=4429)
        return

    chat_session_id: Optional[str] = None
    vehicle_type: Optional[str] = None
    image_count = 0
    rl_window_start = time.time()
    rl_count = 0

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_FRAME_BYTES:
                await _send(websocket, {"type": "error", "detail": "frame_too_large"})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(websocket, {"type": "error", "detail": "invalid_json"})
                continue

            mtype = msg.get("type")

            if mtype == "typing":
                continue

            if mtype == "close":
                break

            if mtype == "start":
                try:
                    start = ChatStart(**{k: v for k, v in msg.items() if k != "type"})
                except ValidationError:
                    await _send(websocket, {"type": "error", "detail": "invalid_start"})
                    continue
                chat_session_id = str(uuid.uuid4())
                vehicle_type = start.vehicle_type
                image_count = 0
                await _send(
                    websocket, {"type": "session", "session_id": chat_session_id}
                )
                continue

            if mtype != "message":
                await _send(websocket, {"type": "error", "detail": "unknown_type"})
                continue

            if not chat_session_id or not vehicle_type:
                await _send(websocket, {"type": "need_vehicle_type"})
                continue

            now = time.time()
            if now - rl_window_start > RATE_LIMIT_WINDOW_SECONDS:
                rl_window_start = now
                rl_count = 0
            rl_count += 1
            if rl_count > RATE_LIMIT_MESSAGES:
                await _send(websocket, {"type": "error", "detail": "rate_limited"})
                continue

            try:
                body = ChatMessageIn(**{k: v for k, v in msg.items() if k != "type"})
            except ValidationError as e:
                await _send(
                    websocket, {"type": "error", "detail": e.errors()[0]["msg"]}
                )
                continue

            if body.session_id != chat_session_id:
                await _send(websocket, {"type": "error", "detail": "session_mismatch"})
                continue

            if image_count + len(body.image_keys) > AI_MAX_IMAGES_PER_SESSION:
                await _send(websocket, {"type": "image_limit"})
                continue

            media = []
            owned = True
            for key in body.image_keys:
                if not storage.is_owned_key(user_id, chat_session_id, key):
                    owned = False
                    break
                media.append(
                    {"url": await storage.generate_ai_get_url(key), "type": "image"}
                )
            if not owned:
                await _send(websocket, {"type": "error", "detail": "image_not_owned"})
                continue

            payload = {
                "user_id": user_id,
                "session_id": chat_session_id,
                "vehicle_type": vehicle_type,
                "query": body.query,
                "media": media,
            }

            if body.stream:
                if await _relay_diagnose_stream(websocket, payload):
                    image_count += len(body.image_keys)
                continue

            try:
                result = await ai_client.forward_diagnose(payload)
            except Exception as exc:  # noqa: BLE001
                detail = getattr(exc, "detail", "ai_unavailable")
                if isinstance(detail, str) and "IMAGE_LIMIT_REACHED" in detail:
                    await _send(websocket, {"type": "image_limit"})
                else:
                    await _send(websocket, {"type": "error", "detail": str(detail)})
                continue

            image_count += len(body.image_keys)
            await _send(
                websocket,
                {
                    "type": "assistant",
                    "kind": result.get("kind"),
                    "markdown": result.get("answer_markdown"),
                    "safety_alert": result.get("safety_alert"),
                    "images": result.get("images") or [],
                },
            )

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"AI diagnostic WS error: {e}")
    finally:
        await manager.disconnect(user_id, websocket)
        if chat_session_id:
            try:
                await storage.delete_ai_session_media(user_id, chat_session_id)
                await ai_client.close_session(chat_session_id)
            except Exception as ce:  # noqa: BLE001
                print(f"AI diagnostic WS cleanup error: {ce}")
