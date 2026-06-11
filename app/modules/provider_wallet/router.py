"""
Provider wallet endpoints (driver / tow / mechanic / service-center app).

  GET  /provider/wallet                balance + status (lazily created)
  GET  /provider/wallet/transactions   paginated history
  POST /provider/wallet/topup          add money via gateway (card / UPI)
  POST /provider/wallet/cashout        withdraw to the registered bank account

Paying the driver acceptance fee from this wallet is exposed on the trip
accept-and-pay path (channel="provider_wallet").
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.idempotency import IdempotencyGuard, idempotent
from app.core.models import (
    ProviderWallet,
    ProviderWalletTransaction,
    ProviderWalletPublic,
    ProviderWalletTransactionPublic,
    ProviderTopupRequest,
    ProviderCashoutRequest,
    PayoutRecordPublic,
)
from app.core.security import get_current_provider, ProviderContext
from app.modules.provider_wallet import service as pw_service
from app.modules.payout import service as payout_service

router = APIRouter(prefix="/provider/wallet", tags=["Provider Wallet"])


@router.get("", response_model=ProviderWalletPublic)
def get_provider_wallet(
    session: Session = Depends(get_session),
    ctx: ProviderContext = Depends(get_current_provider),
):
    wallet = pw_service.get_or_create_provider_wallet(
        session, ctx.user.id, ctx.provider_type
    )
    session.commit()
    session.refresh(wallet)
    return wallet


@router.get("/transactions", response_model=List[ProviderWalletTransactionPublic])
def list_provider_transactions(
    session: Session = Depends(get_session),
    ctx: ProviderContext = Depends(get_current_provider),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = session.exec(
        select(ProviderWalletTransaction)
        .where(ProviderWalletTransaction.user_id == ctx.user.id)
        .order_by(
            ProviderWalletTransaction.created_at.desc(),
            ProviderWalletTransaction.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return rows


@router.get("/transactions/{txn_ref}", response_model=ProviderWalletTransactionPublic)
def get_provider_transaction(
    txn_ref: str,
    session: Session = Depends(get_session),
    ctx: ProviderContext = Depends(get_current_provider),
):
    """Fetch one wallet transaction by reference (e.g. poll a top-up status)."""
    txn = session.exec(
        select(ProviderWalletTransaction).where(
            ProviderWalletTransaction.reference_id == txn_ref,
            ProviderWalletTransaction.user_id == ctx.user.id,
        )
    ).first()
    if not txn:
        raise HTTPException(404, "Transaction not found")
    return txn


@router.post("/topup")
def topup_provider_wallet(
    data: ProviderTopupRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    ctx: ProviderContext = Depends(get_current_provider),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    guard: IdempotencyGuard = Depends(idempotent("provider_wallet.topup")),
):
    if guard.cached_response is not None:
        return guard.cached_response

    txn, client_secret = pw_service.create_topup_intent(
        session,
        ctx.user.id,
        ctx.provider_type,
        data.amount,
        method=data.method,
        idempotency_key=idempotency_key,
    )
    if client_secret and txn.gateway_intent_id:
        background_tasks.add_task(
            pw_service.simulate_topup_settlement, txn.gateway_intent_id, txn.amount
        )

    result = {
        "transaction": ProviderWalletTransactionPublic.model_validate(
            txn, from_attributes=True
        ).model_dump(mode="json"),
        "client_secret": client_secret,
    }
    guard.store(result)
    return result


@router.post("/cashout", response_model=PayoutRecordPublic)
def cashout_provider_wallet(
    data: ProviderCashoutRequest,
    session: Session = Depends(get_session),
    ctx: ProviderContext = Depends(get_current_provider),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
):
    """Withdraw money from the provider wallet to the registered bank account."""
    wallet = pw_service.get_or_create_provider_wallet(
        session, ctx.user.id, ctx.provider_type
    )
    session.commit()
    record = payout_service.execute_payout(
        session,
        wallet=wallet,
        amount=data.amount,
        kind="cashout",
        idempotency_key=idempotency_key,
    )
    return record
