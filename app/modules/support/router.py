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
    User,
    SupportTicket,
    SupportTicketCreate,
    SupportTicketResponse,
    SupportTicketDetailResponse,
    SupportMessage,
    SupportMessageCreate,
    SupportMessageResponse,
    SupportAttachment,
    SupportAttachmentResponse,
    SupportFAQ,
    SupportFAQResponse,
)
from app.core.security import get_current_user, get_current_user_no_member
from app.modules.support import service as support_service
from app.modules.support.attachments import upload_support_attachment
from app.modules.support.ws_manager import manager


router = APIRouter(prefix="/support", tags=["Support & Help"])


def _role_prefix(user: User) -> str:
    role = (user.role or "user").lower()
    return {
        "user": "u",
        "driver": "drv",
        "tow_truck_driver": "tow",
        "mechanic": "mec",
        "service_center": "sc",
        "admin": "adm",
    }.get(role, role[:3])


# ---------------------------------------------------------------------------
# FAQ (pre-built solutions) — read-only listing for user/driver apps.
# Admin CRUD lives in app/modules/admin/support_router.py.
# ---------------------------------------------------------------------------


@router.get("/faqs", response_model=List[SupportFAQResponse])
def list_faqs(
    service_type: Optional[str] = None,
    category: Optional[str] = None,
    session: Session = Depends(get_session),
    _: User = Depends(get_current_user_no_member),
):
    q = select(SupportFAQ).where(SupportFAQ.is_active == True)  # noqa: E712
    if service_type:
        q = q.where(SupportFAQ.service_type == service_type)
    if category:
        q = q.where(SupportFAQ.category == category)
    q = q.order_by(SupportFAQ.display_order, SupportFAQ.id)
    return session.exec(q).all()


# ---------------------------------------------------------------------------
# Ticket endpoints
# ---------------------------------------------------------------------------


@router.post("/tickets", response_model=SupportTicketResponse)
def create_ticket(
    payload: SupportTicketCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user_no_member),
):
    """Create a new support ticket, optionally linked to a service."""
    ticket = support_service.create_ticket(session, current_user, payload)
    return ticket


@router.get("/my-tickets", response_model=List[SupportTicketResponse])
def get_my_tickets(
    status: Optional[str] = None,
    service_type: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user_no_member),
):
    q = (
        select(SupportTicket)
        .where(SupportTicket.user_id == current_user.id)
        .order_by(desc(SupportTicket.created_at))
    )
    if status:
        q = q.where(SupportTicket.status == status)
    if service_type:
        q = q.where(SupportTicket.service_type == service_type)
    return session.exec(q.offset(skip).limit(limit)).all()


@router.get("/tickets/{ticket_db_id}", response_model=SupportTicketDetailResponse)
def get_ticket_detail(
    ticket_db_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user_no_member),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    support_service.assert_can_view_ticket(ticket, current_user)

    messages = session.exec(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket.id)
        .order_by(SupportMessage.created_at)
    ).all()
    attachments = session.exec(
        select(SupportAttachment).where(SupportAttachment.ticket_id == ticket.id)
    ).all()

    base = ticket.model_dump()
    base["messages"] = [support_service.serialize_message(m) for m in messages]
    base["attachments"] = [support_service.serialize_attachment(a) for a in attachments]
    return base


@router.post("/tickets/{ticket_db_id}/messages", response_model=SupportMessageResponse)
async def post_message(
    ticket_db_id: int,
    payload: SupportMessageCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user_no_member),
):
    """HTTP fallback for sending a message (WebSocket is the primary path)."""
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    support_service.assert_can_view_ticket(ticket, current_user)

    sender_role = current_user.role or "user"
    sender_id = current_user.email if sender_role == "admin" else str(current_user.id)
    msg = support_service.append_message(
        session=session,
        ticket=ticket,
        sender_role=sender_role,
        sender_id=sender_id,
        body=payload.body,
        attachment_ids=payload.attachment_ids,
    )
    session.refresh(msg)

    # Broadcast over WS to any connected admin or other tabs
    await manager.broadcast(
        ticket_db_id,
        {
            "type": "message",
            "message": support_service.serialize_message(msg),
            "ticket_status": ticket.status,
        },
    )
    return support_service.serialize_message(msg)


@router.post(
    "/tickets/{ticket_db_id}/attachments",
    response_model=List[SupportAttachmentResponse],
)
async def upload_attachments(
    ticket_db_id: int,
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user_no_member),
):
    """
    Upload one or more files (image/document, <= 10MB each) for a ticket.
    Returns attachment objects with ids; client then sends a message
    (WS or HTTP) referencing these ids.
    """
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    support_service.assert_can_view_ticket(ticket, current_user)
    if ticket.status == "closed":
        raise HTTPException(409, "Ticket is closed")

    uploader_prefix = _role_prefix(current_user)
    results: List[SupportAttachment] = []
    for f in files:
        meta = await upload_support_attachment(f, ticket.id, uploader_prefix)
        att = SupportAttachment(
            ticket_id=ticket.id,
            message_id=None,
            file_url=meta["file_url"],
            r2_key=meta["r2_key"],
            file_name=meta["file_name"],
            file_type=meta["file_type"],
            mime_type=meta["mime_type"],
            file_size=meta["file_size"],
            uploaded_by_role=current_user.role or "user",
            uploaded_by_id=str(current_user.id),
        )
        session.add(att)
        session.commit()
        session.refresh(att)
        results.append(att)
    return [support_service.serialize_attachment(a) for a in results]


@router.post("/tickets/{ticket_db_id}/close")
async def user_close_ticket(
    ticket_db_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user_no_member),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.user_id != current_user.id:
        raise HTTPException(403, "Not your ticket")
    if ticket.status == "closed":
        return {"message": "Already closed"}

    support_service.close_ticket(
        session, ticket, closed_by="user", system_note="Ticket closed by user."
    )
    await manager.close_room(ticket_db_id, reason="user")
    return {"message": "Ticket closed"}
