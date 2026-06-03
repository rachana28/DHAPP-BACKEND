from datetime import datetime
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    UploadFile,
    File,
)
from sqlmodel import Session, select, desc

from app.core.database import get_session
from app.core.models import (
    SupportTicket,
    SupportTicketResponse,
    SupportTicketDetailResponse,
    SupportTicketStatusUpdate,
    SupportMessage,
    SupportMessageCreate,
    SupportMessageResponse,
    SupportAttachment,
    SupportAttachmentResponse,
    SupportFAQ,
    SupportFAQCreate,
    SupportFAQUpdate,
    SupportFAQResponse,
    User,
)
from app.core.security import get_current_admin
from app.modules.support import service as support_service
from app.modules.support.attachments import upload_support_attachment
from app.modules.support.ws_manager import manager
from app.utils.storage import delete_r2_keys
from app.utils.notifications import send_push_notification


# ---------------------------------------------------------------------------
# Ticket admin router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/admin/support",
    tags=["Admin Support"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/tickets", response_model=List[SupportTicketResponse])
def list_tickets(
    status: Optional[str] = None,
    category: Optional[str] = None,
    service_type: Optional[str] = None,
    raised_by_role: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
):
    q = select(SupportTicket).order_by(desc(SupportTicket.created_at))
    if status:
        q = q.where(SupportTicket.status == status)
    if category:
        q = q.where(SupportTicket.category == category)
    if service_type:
        q = q.where(SupportTicket.service_type == service_type)
    if raised_by_role:
        q = q.where(SupportTicket.raised_by_role == raised_by_role)
    if search:
        q = q.where(SupportTicket.ticket_id.contains(search))
    return session.exec(q.offset(skip).limit(limit)).all()


@router.get("/tickets/{ticket_db_id}", response_model=SupportTicketDetailResponse)
def get_ticket_detail(
    ticket_db_id: int,
    session: Session = Depends(get_session),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    messages = session.exec(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket.id)
        .order_by(SupportMessage.created_at)
    ).all()
    attachments = session.exec(
        select(SupportAttachment).where(SupportAttachment.ticket_id == ticket.id)
    ).all()

    live = support_service.fetch_live_service(
        session, ticket.service_type, ticket.service_ref_id
    )

    base = ticket.model_dump()
    base["messages"] = [support_service.serialize_message(m) for m in messages]
    base["attachments"] = [support_service.serialize_attachment(a) for a in attachments]
    if live is not None:
        base["service_snapshot"] = {
            "snapshot_at_creation": ticket.service_snapshot,
            "current": live,
        }
    return base


@router.post("/tickets/{ticket_db_id}/messages", response_model=SupportMessageResponse)
async def admin_post_message(
    ticket_db_id: int,
    payload: SupportMessageCreate,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    msg = support_service.append_message(
        session=session,
        ticket=ticket,
        sender_role="admin",
        sender_id=current_admin.email or str(current_admin.id),
        body=payload.body,
        attachment_ids=payload.attachment_ids,
    )
    session.refresh(msg)

    await manager.broadcast(
        ticket_db_id,
        {
            "type": "message",
            "message": support_service.serialize_message(msg),
            "ticket_status": ticket.status,
        },
    )

    if not manager.is_any_customer_online(ticket_db_id):
        try:
            send_push_notification(
                session=session,
                user_ids=support_service.get_customer_user_ids(ticket),
                title="New support reply",
                body=(msg.body[:120] if msg.body else "You have a new message."),
                data={"type": "support_message", "ticket_id": ticket_db_id},
            )
        except Exception as e:
            print(f"Support push error: {e}")

    return support_service.serialize_message(msg)


@router.post(
    "/tickets/{ticket_db_id}/attachments",
    response_model=List[SupportAttachmentResponse],
)
async def admin_upload_attachments(
    ticket_db_id: int,
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.status == "closed":
        raise HTTPException(409, "Ticket is closed")

    results: List[SupportAttachment] = []
    for f in files:
        meta = await upload_support_attachment(f, ticket.id, "admin")
        att = SupportAttachment(
            ticket_id=ticket.id,
            message_id=None,
            file_url=meta["file_url"],
            r2_key=meta["r2_key"],
            file_name=meta["file_name"],
            file_type=meta["file_type"],
            mime_type=meta["mime_type"],
            file_size=meta["file_size"],
            uploaded_by_role="admin",
            uploaded_by_id=current_admin.email or str(current_admin.id),
        )
        session.add(att)
        session.commit()
        session.refresh(att)
        results.append(att)

    return [support_service.serialize_attachment(a) for a in results]


@router.patch("/tickets/{ticket_db_id}/status")
async def update_ticket_status(
    ticket_db_id: int,
    payload: SupportTicketStatusUpdate,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    previous_status = ticket.status

    if payload.status == "resolved":
        payload.status = "closed"


    if previous_status == "closed" and payload.status != "closed":
        raise HTTPException(
            409, "Closed tickets cannot be reopened. Ask the user to open a new one."
        )

    ticket.status = payload.status
    ticket.updated_at = datetime.utcnow()
    if payload.admin_response is not None:
        ticket.admin_response = payload.admin_response

    if payload.status == "closed":
        ticket.closed_at = datetime.utcnow()
        ticket.closed_by = "admin"

    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    if payload.status == "closed" and previous_status != "closed":
        sys_msg = SupportMessage(
            ticket_id=ticket.id,
            sender_role="system",
            sender_id="system",
            body="Ticket closed by admin.",
            is_system=True,
        )
        session.add(sys_msg)
        session.commit()
        # Tear down WS room AFTER DB writes succeed so clients see a
        # consistent closed state when they reconnect.
        await manager.close_room(ticket_db_id, reason="admin")

    return {"message": "Ticket updated", "ticket": ticket}


@router.delete("/tickets/{ticket_db_id}")
async def delete_ticket(
    ticket_db_id: int,
    session: Session = Depends(get_session),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    keys = [a.r2_key for a in ticket.attachments if a.r2_key]

    # Commit DB delete first. If it fails we haven't disturbed live clients.
    session.delete(ticket)
    session.commit()

    # Now safe to tear down WS connections and clean up R2.
    await manager.close_room(ticket_db_id, reason="deleted")
    if keys:
        try:
            await delete_r2_keys(keys)
        except Exception as e:
            print(f"R2 cleanup failed for ticket {ticket_db_id}: {e}")

    return {"message": "Ticket deleted"}


@router.delete("/messages/{msg_id}")
async def soft_delete_message(
    msg_id: int,
    session: Session = Depends(get_session),
):
    msg = session.get(SupportMessage, msg_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    if msg.is_deleted:
        return {"message": "Already deleted"}
    msg.is_deleted = True
    session.add(msg)
    session.commit()
    await manager.broadcast(
        msg.ticket_id, {"type": "deleted", "scope": "message", "id": msg_id}
    )
    return {"message": "Message deleted"}


@router.delete("/attachments/{att_id}")
async def delete_attachment(
    att_id: int,
    session: Session = Depends(get_session),
):
    att = session.get(SupportAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    ticket_id = att.ticket_id
    key = att.r2_key

    session.delete(att)
    session.commit()

    try:
        if key:
            await delete_r2_keys([key])
    except Exception as e:
        print(f"R2 attachment delete error: {e}")

    await manager.broadcast(
        ticket_id, {"type": "deleted", "scope": "attachment", "id": att_id}
    )
    return {"message": "Attachment deleted"}


# ---------------------------------------------------------------------------
# FAQ admin router (CRUD)
# ---------------------------------------------------------------------------

faq_router = APIRouter(
    prefix="/admin/support/faqs",
    tags=["Admin Support FAQ"],
    dependencies=[Depends(get_current_admin)],
)


@faq_router.get("", response_model=List[SupportFAQResponse])
def admin_list_faqs(
    service_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    q = select(SupportFAQ)
    if service_type:
        q = q.where(SupportFAQ.service_type == service_type)
    if is_active is not None:
        q = q.where(SupportFAQ.is_active == is_active)
    q = q.order_by(SupportFAQ.display_order, SupportFAQ.id)
    return session.exec(q.offset(skip).limit(limit)).all()


@faq_router.post("", response_model=SupportFAQResponse)
def admin_create_faq(
    payload: SupportFAQCreate,
    session: Session = Depends(get_session),
):
    faq = SupportFAQ(**payload.model_dump())
    session.add(faq)
    session.commit()
    session.refresh(faq)
    return faq


@faq_router.patch("/{faq_id}", response_model=SupportFAQResponse)
def admin_update_faq(
    faq_id: int,
    payload: SupportFAQUpdate,
    session: Session = Depends(get_session),
):
    faq = session.get(SupportFAQ, faq_id)
    if not faq:
        raise HTTPException(404, "FAQ not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(faq, key, value)
    faq.updated_at = datetime.utcnow()
    session.add(faq)
    session.commit()
    session.refresh(faq)
    return faq


@faq_router.delete("/{faq_id}")
def admin_delete_faq(
    faq_id: int,
    session: Session = Depends(get_session),
):
    faq = session.get(SupportFAQ, faq_id)
    if not faq:
        raise HTTPException(404, "FAQ not found")
    session.delete(faq)
    session.commit()
    return {"message": "FAQ deleted"}
