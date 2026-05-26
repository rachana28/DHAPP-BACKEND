import json
import time
from typing import Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from jose import JWTError, jwt
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.models import User, SupportTicket
from app.core.security import SECRET_KEY, ALGORITHM
from app.modules.support import service as support_service
from app.modules.support.ws_manager import manager
from app.utils.notifications import send_push_notification

router = APIRouter(prefix="/support", tags=["Support Chat (WS)"])

# Per-connection rate limit: max 10 messages per 10s window
RATE_LIMIT_MESSAGES = 10
RATE_LIMIT_WINDOW_SECONDS = 10


def _resolve_user_from_token(
    token: str, session: Session
) -> Optional[Tuple[User, str]]:
    """Returns (User, role) or None if invalid."""
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


@router.websocket("/ws/{ticket_db_id}")
async def support_chat_ws(
    websocket: WebSocket,
    ticket_db_id: int,
    token: Optional[str] = None,
    session: Session = Depends(get_session),
):
    # 1. Auth via query param
    if not token:
        await websocket.close(code=4401)
        return

    resolved = _resolve_user_from_token(token, session)
    if not resolved:
        await websocket.close(code=4401)
        return
    current_user, role = resolved

    # 2. Load ticket and verify access
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        await websocket.close(code=4404)
        return

    if role != "admin" and ticket.user_id != current_user.id:
        await websocket.close(code=4403)
        return

    # 3. Closed-ticket: send a final frame and disconnect immediately
    if ticket.status == "closed":
        await websocket.accept()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "closed",
                    "reason": ticket.closed_by or "closed",
                    "closed_at": str(ticket.closed_at) if ticket.closed_at else None,
                }
            )
        )
        await websocket.close()
        return

    # 4. Accept and register
    identity = current_user.email if role == "admin" else str(current_user.id)
    await websocket.accept()
    accepted = await manager.connect(ticket_db_id, role, websocket, identity=identity)
    if not accepted:
        # Too many concurrent connections from this identity for this ticket
        await websocket.send_text(
            json.dumps({"type": "error", "detail": "too_many_connections"})
        )
        await websocket.close(code=4429)
        return

    # Send a hello frame with current ticket status
    await websocket.send_text(
        json.dumps(
            {
                "type": "hello",
                "ticket_id": ticket_db_id,
                "ticket_status": ticket.status,
            }
        )
    )

    rl_window_start = time.time()
    rl_count = 0

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "invalid_json"})
                )
                continue

            mtype = msg.get("type")

            # ping / typing / read are lightweight passthroughs
            if mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue
            if mtype == "typing":
                await manager.broadcast(
                    ticket_db_id,
                    {"type": "typing", "from_role": role},
                    exclude_ws=websocket,
                )
                continue
            if mtype == "read":
                up_to = msg.get("up_to_message_id")
                await manager.broadcast(
                    ticket_db_id,
                    {
                        "type": "read",
                        "from_role": role,
                        "up_to_message_id": up_to,
                    },
                    exclude_ws=websocket,
                )
                continue

            if mtype != "message":
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "unknown_type"})
                )
                continue

            # Rate-limit message frames
            now = time.time()
            if now - rl_window_start > RATE_LIMIT_WINDOW_SECONDS:
                rl_window_start = now
                rl_count = 0
            rl_count += 1
            if rl_count > RATE_LIMIT_MESSAGES:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "rate_limited"})
                )
                continue

            body = msg.get("body")
            attachment_ids = msg.get("attachment_ids") or []
            if not isinstance(attachment_ids, list):
                attachment_ids = []

            # Re-fetch ticket fresh; status may have changed in another tab
            ticket = session.get(SupportTicket, ticket_db_id)
            if not ticket or ticket.status == "closed":
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "ticket_closed"})
                )
                await manager.close_room(ticket_db_id, reason="closed")
                break

            sender_id = current_user.email if role == "admin" else str(current_user.id)

            try:
                saved = support_service.append_message(
                    session=session,
                    ticket=ticket,
                    sender_role=role,
                    sender_id=sender_id,
                    body=body,
                    attachment_ids=[int(a) for a in attachment_ids],
                )
            except Exception as e:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": str(e)})
                )
                continue

            session.refresh(saved)
            payload = {
                "type": "message",
                "message": support_service.serialize_message(saved),
                "ticket_status": ticket.status,
            }
            await manager.broadcast(ticket_db_id, payload)

            # Offline push: if admin sent, push to the customer if they
            # have no active WS in the room. Vice-versa, customer ->
            # admin notifications are typically handled by an admin dashboard
            # and are out of scope for Expo push.
            if role == "admin":
                customer_online = manager.is_any_customer_online(ticket_db_id)
                if not customer_online:
                    try:
                        send_push_notification(
                            session=session,
                            user_ids=support_service.get_customer_user_ids(ticket),
                            title="New support reply",
                            body=(
                                saved.body[:120]
                                if saved.body
                                else "You have a new message."
                            ),
                            data={
                                "type": "support_message",
                                "ticket_id": ticket_db_id,
                            },
                        )
                    except Exception as ne:
                        print(f"Support push error: {ne}")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Support WS error: {e}")
    finally:
        await manager.disconnect(ticket_db_id, role, websocket, identity=identity)
