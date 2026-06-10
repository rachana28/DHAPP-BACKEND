"""
Admin merchant-bank ledger (READ-ONLY).

Exposes the single physical-money ledger that tracks the platform merchant bank
account balance and every credit/debit with its service/payment provenance.
GET endpoints only — the ledger is system-maintained and never edited by hand.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.security import get_current_admin
from app.core.models import MerchantBankLedger, MerchantBankLedgerPublic

router = APIRouter(
    prefix="/admin/merchant-bank",
    tags=["Admin Merchant Bank"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/summary")
def merchant_bank_summary(session: Session = Depends(get_session)):
    """Current merchant-bank balances: total, provider-owed and user-float."""
    from app.services import merchant_bank

    balances = merchant_bank.current_balances(session)
    return {
        "running_balance": balances["running_balance"],
        "provider_owed_balance": balances["provider_owed_balance"],
        "user_float_balance": balances["user_float_balance"],
    }


@router.get("/transactions", response_model=List[MerchantBankLedgerPublic])
def merchant_bank_transactions(
    session: Session = Depends(get_session),
    tag: Optional[str] = Query(
        None, description="provider_owed|user_float|payout|cashout|refund"
    ),
    provider_type: Optional[str] = None,
    service_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginated merchant-bank ledger, newest first; optional filters."""
    q = select(MerchantBankLedger)
    if tag:
        q = q.where(MerchantBankLedger.tag == tag)
    if provider_type:
        q = q.where(MerchantBankLedger.provider_type == provider_type)
    if service_type:
        q = q.where(MerchantBankLedger.service_type == service_type)
    q = q.order_by(MerchantBankLedger.id.desc()).offset(offset).limit(limit)
    return session.exec(q).all()
