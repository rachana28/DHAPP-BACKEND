"""Server-owned notification inbox API.

Exposes the durable notification history written by the push-send service:
keyset-paginated listing, read-state mutation, and an unread badge count.
All queries are scoped to the authenticated user.
"""

import base64
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, tuple_
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.models import Notification, NotificationResponse, User, _now_ist_naive
from app.core.security import get_current_user

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
    dependencies=[Depends(get_current_user)],
)


def _encode_cursor(created_at: datetime, notif_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{notif_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_str, id_str = raw.rsplit("|", 1)
        return datetime.fromisoformat(created_str), uuid.UUID(id_str)
    except Exception:
        raise HTTPException(400, "Invalid cursor")


def _to_response(n: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=n.id,
        type=n.type,
        title=n.title,
        body=n.body,
        detail=n.detail,
        data=n.data or {},
        important=n.important,
        read=n.read_at is not None,
        read_at=n.read_at,
        created_at=n.created_at,
    )


def _unread_count(session: Session, user_id: uuid.UUID) -> int:
    return session.exec(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    ).one()


@router.get("")
def list_notifications(
    limit: int = Query(30, ge=1, le=100),
    cursor: Optional[str] = None,
    unread_only: bool = False,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    query = select(Notification).where(Notification.user_id == current_user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if cursor:
        c_created, c_id = _decode_cursor(cursor)
        query = query.where(
            tuple_(Notification.created_at, Notification.id) < (c_created, c_id)
        )
    query = query.order_by(
        Notification.created_at.desc(), Notification.id.desc()
    ).limit(limit + 1)

    rows = session.exec(query).all()

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return {
        "items": [_to_response(n) for n in rows],
        "next_cursor": next_cursor,
        "unread_count": _unread_count(session, current_user.id),
    }


@router.get("/unread-count")
def unread_count(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return {"unread_count": _unread_count(session, current_user.id)}


@router.post("/read-all")
def mark_all_read(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    rows = session.exec(
        select(Notification).where(
            Notification.user_id == current_user.id,
            Notification.read_at.is_(None),
        )
    ).all()
    now = _now_ist_naive()
    for n in rows:
        n.read_at = now
        session.add(n)
    session.commit()
    return {"updated": len(rows)}


def _get_owned(
    session: Session, notif_id: uuid.UUID, current_user: User
) -> Notification:
    notif = session.get(Notification, notif_id)
    if not notif:
        raise HTTPException(404, "Notification not found")
    if notif.user_id != current_user.id:
        raise HTTPException(403, "Not authorized to access this notification")
    return notif


@router.post("/{notif_id}/read")
def mark_read(
    notif_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    notif = _get_owned(session, notif_id, current_user)
    if notif.read_at is None:
        notif.read_at = _now_ist_naive()
        session.add(notif)
        session.commit()
        session.refresh(notif)
    return _to_response(notif)


@router.delete("/{notif_id}")
def delete_notification(
    notif_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    notif = _get_owned(session, notif_id, current_user)
    session.delete(notif)
    session.commit()
    return {"deleted": str(notif_id)}
