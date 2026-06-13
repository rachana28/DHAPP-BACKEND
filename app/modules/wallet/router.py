"""
Wallet endpoints (user app).

  GET  /wallet                  balance + status (lazily creates the wallet)
  GET  /wallet/transactions     paginated transaction history
  POST /wallet/topup            start a gateway top-up; returns client_secret
  POST /wallet/admin/credit     admin-only promo/goodwill credit

Paying a booking FROM the wallet is done via the centralized payments module
(`POST /payments/create-intent` with channel="wallet").
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.idempotency import IdempotencyGuard, idempotent
from app.core.models import (
    User,
    Wallet,
    WalletTransaction,
    WalletPublic,
    WalletTransactionPublic,
    WalletTopupRequest,
    WalletAdminCreditRequest,
)
from app.core.security import (
    get_current_user,
    get_current_admin,
    get_current_user_no_member,
)
from app.modules.wallet import service as wallet_service

# Center-members have no wallet — blocked at the router level.
router = APIRouter(
    prefix="/wallet",
    tags=["Wallet"],
    dependencies=[Depends(get_current_user_no_member)],
)


@router.get("", response_model=WalletPublic)
def get_wallet(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    wallet = wallet_service.get_or_create_wallet(session, current_user.id)
    session.commit()
    session.refresh(wallet)
    return wallet


@router.get("/transactions", response_model=List[WalletTransactionPublic])
def list_transactions(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = session.exec(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == current_user.id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return rows


@router.post("/topup")
def topup_wallet(
    data: WalletTopupRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    guard: IdempotencyGuard = Depends(idempotent("wallet.topup")),
):
    # Replay: same Idempotency-Key within the TTL returns the cached response.
    if guard.cached_response is not None:
        return guard.cached_response

    txn, client_secret = wallet_service.create_topup_intent(
        session, current_user, data.amount, idempotency_key=idempotency_key
    )

    # Mock async settlement — only for a freshly created intent (a DB-level
    # idempotency replay returns client_secret=None). A real gateway calls
    # /payments/webhook out of band.
    if client_secret and txn.gateway_intent_id:
        background_tasks.add_task(
            wallet_service.simulate_topup_settlement, txn.gateway_intent_id, txn.amount
        )

    result = {
        "transaction": WalletTransactionPublic.model_validate(
            txn, from_attributes=True
        ).model_dump(mode="json"),
        "client_secret": client_secret,
    }
    guard.store(result)
    return result


@router.post("/admin/credit", response_model=WalletTransactionPublic)
def admin_credit_wallet(
    data: WalletAdminCreditRequest,
    session: Session = Depends(get_session),
    admin: User = Depends(get_current_admin),
):
    target = session.exec(
        select(User).where(
            User.phone_number == data.user_reference, User.role == "user"
        )
    ).first()
    if not target:
        raise HTTPException(404, "Target user not found")

    txn = wallet_service.credit(
        session,
        target,
        data.amount,
        source=data.source,
        note=data.note,
    )
    session.commit()
    session.refresh(txn)

    from app.services.audit_log import emit_event as audit_emit

    audit_emit(
        "wallet.admin_credit",
        trip_id=None,
        actor="admin",
        actor_id=str(admin.id),
        payload={
            "target_user_id": str(target.id),
            "amount": data.amount,
            "source": data.source,
            "txn_reference": txn.reference_id,
        },
    )
    return txn
