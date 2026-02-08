from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc
from typing import List
import uuid
from datetime import datetime

from app.core.database import get_session
from app.core.models import (
    User,
    SupportTicket,
    SupportTicketCreate,
    SupportTicketResponse,
)
from app.core.security import get_current_user

router = APIRouter(prefix="/support", tags=["Support & Help"])


@router.post("/tickets", response_model=SupportTicketResponse)
def create_ticket(
    ticket_in: SupportTicketCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new support ticket.
    """
    # Generate a readable Ticket ID (e.g., TKT-1234abcd)
    tkt_uid = f"TKT-{str(uuid.uuid4())[:8].upper()}"

    db_ticket = SupportTicket(
        **ticket_in.model_dump(),
        user_id=current_user.id,
        ticket_id=tkt_uid,
        status="open",
    )

    session.add(db_ticket)
    session.commit()
    session.refresh(db_ticket)
    return db_ticket


@router.get("/my-tickets", response_model=List[SupportTicketResponse])
def get_my_tickets(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    View history of my support tickets.
    """
    statement = (
        select(SupportTicket)
        .where(SupportTicket.user_id == current_user.id)
        .order_by(desc(SupportTicket.created_at))
    )
    return session.exec(statement).all()
