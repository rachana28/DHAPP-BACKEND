"""
Admin payout / cash-out records (READ-ONLY).

Exposes the single PayoutRecord table covering both automatic payouts and
provider-initiated cash-outs, including bounced attempts with their reasons.
GET endpoints only — records are system-written and never edited by hand.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.security import get_current_admin
from app.core.models import PayoutRecord, PayoutRecordPublic
from app.utils.id_generator import get_by_reference

router = APIRouter(
    prefix="/admin/payouts",
    tags=["Admin Payouts"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("", response_model=List[PayoutRecordPublic])
def list_payouts(
    session: Session = Depends(get_session),
    kind: Optional[str] = Query(None, description="auto_payout|cashout"),
    status: Optional[str] = Query(None, description="initiated|success|bounced|failed"),
    provider_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginated payout/cash-out records, newest first; optional filters
    (e.g. status=bounced to review bounced amounts)."""
    q = select(PayoutRecord)
    if kind:
        q = q.where(PayoutRecord.kind == kind)
    if status:
        q = q.where(PayoutRecord.status == status)
    if provider_type:
        q = q.where(PayoutRecord.provider_type == provider_type)
    q = q.order_by(PayoutRecord.id.desc()).offset(offset).limit(limit)
    return session.exec(q).all()


@router.get("/summary")
def payout_summary(session: Session = Depends(get_session)):
    """Aggregate counts/amounts by status (incl. total bounced)."""
    rows = session.exec(select(PayoutRecord)).all()
    summary: dict = {}
    for r in rows:
        s = summary.setdefault(r.status, {"count": 0, "amount": 0.0})
        s["count"] += 1
        s["amount"] = round(s["amount"] + r.amount, 2)
    return summary


@router.get("/{payout_ref}", response_model=PayoutRecordPublic)
def get_payout(payout_ref: str, session: Session = Depends(get_session)):
    record = get_by_reference(session, PayoutRecord, payout_ref)
    if not record:
        raise HTTPException(404, "Payout record not found")
    return record
