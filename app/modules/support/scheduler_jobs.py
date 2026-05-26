import asyncio
from datetime import datetime, timedelta
from typing import List

from sqlmodel import Session, select

from app.core.database import engine
from app.core.models import SupportTicket, SupportMessage, SupportAttachment
from app.modules.support.ws_manager import manager
from app.utils.storage import delete_r2_keys
from app.utils.notifications import send_push_notification


INACTIVITY_HOURS = 2
ORPHAN_ATTACHMENT_AGE_MINUTES = 60


async def auto_close_inactive_support_tickets() -> None:
    """Close service-linked tickets with no message for INACTIVITY_HOURS."""
    cutoff = datetime.utcnow() - timedelta(hours=INACTIVITY_HOURS)
    closed_ids: List[int] = []

    with Session(engine) as session:
        candidates = session.exec(
            select(SupportTicket).where(
                SupportTicket.auto_close_enabled == True,  # noqa: E712
                SupportTicket.status != "closed",
                SupportTicket.last_message_at < cutoff,
            )
        ).all()
        if not candidates:
            return

        now = datetime.utcnow()
        for ticket in candidates:
            ticket.status = "closed"
            ticket.closed_at = now
            ticket.closed_by = "auto_inactivity"
            ticket.updated_at = now
            session.add(ticket)

            session.add(
                SupportMessage(
                    ticket_id=ticket.id,
                    sender_role="system",
                    sender_id="system",
                    body="Ticket auto-closed due to 2 hours of inactivity.",
                    is_system=True,
                )
            )
            closed_ids.append(ticket.id)

            if ticket.user_id:
                try:
                    send_push_notification(
                        session=session,
                        user_ids=[ticket.user_id],
                        title="Support ticket closed",
                        body="Your support ticket was auto-closed due to inactivity.",
                        data={"type": "support_closed", "ticket_id": ticket.id},
                    )
                except Exception as e:
                    print(f"Auto-close push error: {e}")

        session.commit()

    # WS rooms — we're already on the event loop, so just await.
    for tid in closed_ids:
        try:
            await manager.close_room(tid, reason="auto_inactivity")
        except Exception as e:
            print(f"WS close room error for {tid}: {e}")

    print(f"[support] auto-closed {len(closed_ids)} inactive ticket(s)")


async def cleanup_orphan_support_attachments() -> None:
    """
    Delete attachments older than ORPHAN_ATTACHMENT_AGE_MINUTES that were
    never linked to a message. Frees both DB rows and R2 storage.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=ORPHAN_ATTACHMENT_AGE_MINUTES)
    keys: List[str] = []

    with Session(engine) as session:
        orphans = session.exec(
            select(SupportAttachment).where(
                SupportAttachment.message_id.is_(None),
                SupportAttachment.created_at < cutoff,
            )
        ).all()
        if not orphans:
            return
        for att in orphans:
            if att.r2_key:
                keys.append(att.r2_key)
            session.delete(att)
        session.commit()

    if keys:
        try:
            await delete_r2_keys(keys)
        except Exception as e:
            print(f"Orphan attachment R2 cleanup error: {e}")

    print(f"[support] cleaned {len(keys)} orphan attachment(s)")
