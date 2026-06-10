"""
Provider wallet logic — drivers / tow / mechanic / service-center.

Distinct from the user ``Wallet`` (app/modules/wallet):
  * Higher cap (₹1 lakh, ``provider_wallet_max_balance``).
  * May carry a NEGATIVE balance after an admin fraud deduction; the deficit is
    recovered automatically out of the next credit (a ``recovery`` txn is
    written for transparency before the remainder raises the balance).
  * Funded automatically when a user pays online (see payments ``_post_success``)
    and drawn down by cash-out / auto-payout and the driver acceptance fee.

Design mirrors wallet/service.py: every mutation runs under a ``FOR UPDATE`` row
lock; ``credit`` / ``debit`` DO NOT commit (caller owns the transaction);
standalone operations (top-up settlement) commit themselves.
"""

from __future__ import annotations

from typing import Optional, Tuple
import uuid

from fastapi import HTTPException
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from app.core.models import (
    ProviderWallet,
    ProviderWalletTransaction,
    SystemConfig,
    User,
)
from app.modules.payments import gateway
from app.services.audit_log import emit_event as audit_emit
from app.utils.id_generator import (
    generate_reference_id,
    PROVIDER_WALLET,
    PROVIDER_WALLET_TXN,
)
from app.utils.time_utils import now_ist

# --- SystemConfig keys + defaults (admin-tunable via /admin/system-config) ---
PROVIDER_WALLET_MAX_BALANCE_KEY = "provider_wallet_max_balance"
PROVIDER_CASHOUT_MIN_KEY = "provider_cashout_min"
PROVIDER_TOPUP_MIN_KEY = "provider_topup_min"
PROVIDER_TOPUP_MAX_KEY = "provider_topup_max"
DEFAULT_PROVIDER_WALLET_MAX_BALANCE = 100000.0  # ₹1 lakh
DEFAULT_PROVIDER_CASHOUT_MIN = 100.0
DEFAULT_PROVIDER_TOPUP_MIN = 1.0
DEFAULT_PROVIDER_TOPUP_MAX = 100000.0

_EPS = 1e-6


def _get_config_float(session: Session, key: str, default: float) -> float:
    cfg = session.get(SystemConfig, key)
    if cfg and cfg.value:
        try:
            return float(cfg.value)
        except (TypeError, ValueError):
            pass
    return default


# --- wallet lifecycle --------------------------------------------------------
def get_or_create_provider_wallet(
    session: Session, user_id: uuid.UUID, provider_type: str
) -> ProviderWallet:
    wallet = session.exec(
        select(ProviderWallet).where(ProviderWallet.user_id == user_id)
    ).first()
    if wallet:
        return wallet
    try:
        with session.begin_nested():
            wallet = ProviderWallet(
                reference_id=generate_reference_id(session, PROVIDER_WALLET),
                user_id=user_id,
                provider_type=provider_type,
            )
            session.add(wallet)
            session.flush()
    except IntegrityError:
        wallet = session.exec(
            select(ProviderWallet).where(ProviderWallet.user_id == user_id)
        ).first()
        if wallet is None:
            raise
    return wallet


def _lock_wallet(
    session: Session, user_id: uuid.UUID, provider_type: str
) -> ProviderWallet:
    get_or_create_provider_wallet(session, user_id, provider_type)
    return session.exec(
        select(ProviderWallet)
        .where(ProviderWallet.user_id == user_id)
        .with_for_update()
    ).first()


def _new_txn(
    session: Session, wallet: ProviderWallet, **kwargs
) -> ProviderWalletTransaction:
    txn = ProviderWalletTransaction(
        reference_id=generate_reference_id(session, PROVIDER_WALLET_TXN),
        wallet_id=wallet.id,
        user_id=wallet.user_id,
        balance_after=wallet.balance,
        **kwargs,
    )
    session.add(txn)
    session.flush()
    return txn


# --- credits / debits (no commit — caller owns the transaction) --------------
def credit(
    session: Session,
    user_id: uuid.UUID,
    provider_type: str,
    amount: float,
    source: str,
    *,
    note: Optional[str] = None,
    payment_reference: Optional[str] = None,
    related_service_type: Optional[str] = None,
    related_service_reference_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    enforce_cap: bool = True,
) -> ProviderWalletTransaction:
    """Credit the provider wallet. If the wallet is in deficit (negative
    balance from a prior admin deduction), the incoming money FIRST repays the
    deficit (a ``recovery`` txn) and only the remainder raises the balance."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Credit amount must be positive")

    wallet = _lock_wallet(session, user_id, provider_type)

    if enforce_cap:
        cap = _get_config_float(
            session,
            PROVIDER_WALLET_MAX_BALANCE_KEY,
            DEFAULT_PROVIDER_WALLET_MAX_BALANCE,
        )
        if wallet.balance + amount > cap + _EPS:
            raise HTTPException(
                400, f"This credit would exceed the wallet balance cap (₹{cap:.0f})"
            )

    primary: Optional[ProviderWalletTransaction] = None

    # Negative-balance recovery: repay the deficit first.
    if wallet.balance < -_EPS:
        recovered = round(min(amount, -wallet.balance), 2)
        wallet.balance = round(wallet.balance + recovered, 2)
        wallet.updated_at = now_ist()
        session.add(wallet)
        primary = _new_txn(
            session,
            wallet,
            type="credit",
            source="recovery",
            amount=recovered,
            status="success",
            payment_reference=payment_reference,
            related_service_type=related_service_type,
            related_service_reference_id=related_service_reference_id,
            note=(note or "") + " (deduction recovery)",
        )
        amount = round(amount - recovered, 2)

    if amount > _EPS:
        wallet.balance = round(wallet.balance + amount, 2)
        wallet.updated_at = now_ist()
        session.add(wallet)
        primary = _new_txn(
            session,
            wallet,
            type="credit",
            source=source,
            amount=amount,
            status="success",
            note=note,
            payment_reference=payment_reference,
            related_service_type=related_service_type,
            related_service_reference_id=related_service_reference_id,
            idempotency_key=idempotency_key,
        )

    return primary


def debit(
    session: Session,
    user_id: uuid.UUID,
    provider_type: str,
    amount: float,
    source: str,
    *,
    note: Optional[str] = None,
    payment_reference: Optional[str] = None,
    payout_reference: Optional[str] = None,
    related_service_type: Optional[str] = None,
    related_service_reference_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ProviderWalletTransaction:
    """Debit the provider wallet (cash-out, payout, acceptance fee). Requires a
    sufficient POSITIVE balance — fraud guard for withdrawals."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Debit amount must be positive")

    wallet = _lock_wallet(session, user_id, provider_type)
    if not wallet.is_active:
        raise HTTPException(403, "Wallet is inactive")
    if wallet.balance + _EPS < amount:
        raise HTTPException(400, "Insufficient wallet balance")

    wallet.balance = round(wallet.balance - amount, 2)
    wallet.updated_at = now_ist()
    session.add(wallet)
    return _new_txn(
        session,
        wallet,
        type="debit",
        source=source,
        amount=amount,
        status="success",
        note=note,
        payment_reference=payment_reference,
        payout_reference=payout_reference,
        related_service_type=related_service_type,
        related_service_reference_id=related_service_reference_id,
        idempotency_key=idempotency_key,
    )


def admin_deduct(
    session: Session,
    user_id: uuid.UUID,
    provider_type: str,
    amount: float,
    *,
    note: Optional[str] = None,
) -> ProviderWalletTransaction:
    """Admin fraud/issue deduction. ALLOWED to push the balance negative — the
    deficit is recovered out of future credits (see ``credit``)."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Deduction amount must be positive")
    wallet = _lock_wallet(session, user_id, provider_type)
    wallet.balance = round(wallet.balance - amount, 2)
    wallet.updated_at = now_ist()
    session.add(wallet)
    txn = _new_txn(
        session,
        wallet,
        type="debit",
        source="admin_deduction",
        amount=amount,
        status="success",
        note=note or "Admin deduction (fraud/issue)",
    )
    session.commit()
    session.refresh(txn)
    audit_emit(
        "provider_wallet.admin_deducted",
        trip_id=None,
        actor="admin",
        payload={
            "wallet_reference": wallet.reference_id,
            "amount": amount,
            "balance_after": wallet.balance,
            "note": note,
        },
        severity="warning",
    )
    return txn


def debit_clawback(
    session: Session,
    user_id: uuid.UUID,
    provider_type: str,
    amount: float,
    *,
    payment_reference: Optional[str] = None,
    related_service_type: Optional[str] = None,
    related_service_reference_id: Optional[str] = None,
    note: Optional[str] = None,
) -> ProviderWalletTransaction:
    """Claw back money from the provider wallet when a payment is refunded.
    ALLOWED to push the balance negative (the provider may already have been
    paid out); the deficit is recovered out of future credits."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Clawback amount must be positive")
    wallet = _lock_wallet(session, user_id, provider_type)
    wallet.balance = round(wallet.balance - amount, 2)
    wallet.updated_at = now_ist()
    session.add(wallet)
    return _new_txn(
        session,
        wallet,
        type="debit",
        source="reversal",
        amount=amount,
        status="success",
        payment_reference=payment_reference,
        related_service_type=related_service_type,
        related_service_reference_id=related_service_reference_id,
        note=note or "Refund clawback",
    )


def reverse_debit(
    session: Session,
    user_id: uuid.UUID,
    provider_type: str,
    amount: float,
    *,
    payout_reference: Optional[str] = None,
    note: Optional[str] = None,
) -> ProviderWalletTransaction:
    """Re-credit the wallet after a bounced payout/cash-out (money never left).
    Bypasses the cap and deficit-recovery split — it is a pure reversal."""
    amount = round(float(amount), 2)
    wallet = _lock_wallet(session, user_id, provider_type)
    wallet.balance = round(wallet.balance + amount, 2)
    wallet.updated_at = now_ist()
    session.add(wallet)
    return _new_txn(
        session,
        wallet,
        type="credit",
        source="reversal",
        amount=amount,
        status="success",
        payout_reference=payout_reference,
        note=note or "Payout bounce reversal",
    )


# --- top-up (via gateway) ----------------------------------------------------
def create_topup_intent(
    session: Session,
    ctx_user_id: uuid.UUID,
    provider_type: str,
    amount: float,
    method: str = "card",
    idempotency_key: Optional[str] = None,
) -> Tuple[ProviderWalletTransaction, Optional[str]]:
    """Start a provider wallet top-up: a gateway intent + a PENDING credit txn.
    Settlement arrives on the gateway webhook (confirm_topup)."""
    if idempotency_key:
        existing = session.exec(
            select(ProviderWalletTransaction).where(
                ProviderWalletTransaction.idempotency_key == idempotency_key,
                ProviderWalletTransaction.source == "topup",
            )
        ).first()
        if existing:
            return existing, None

    amount = round(float(amount), 2)
    min_t = _get_config_float(
        session, PROVIDER_TOPUP_MIN_KEY, DEFAULT_PROVIDER_TOPUP_MIN
    )
    max_t = _get_config_float(
        session, PROVIDER_TOPUP_MAX_KEY, DEFAULT_PROVIDER_TOPUP_MAX
    )
    cap = _get_config_float(
        session, PROVIDER_WALLET_MAX_BALANCE_KEY, DEFAULT_PROVIDER_WALLET_MAX_BALANCE
    )
    if amount < min_t - _EPS:
        raise HTTPException(400, f"Minimum top-up is ₹{min_t:.0f}")
    if amount > max_t + _EPS:
        raise HTTPException(400, f"Maximum top-up is ₹{max_t:.0f}")

    wallet = get_or_create_provider_wallet(session, ctx_user_id, provider_type)
    if not wallet.is_active:
        raise HTTPException(403, "Wallet is inactive")
    if wallet.balance + amount > cap + _EPS:
        raise HTTPException(
            400, f"This top-up would exceed your wallet cap (₹{cap:.0f})"
        )

    intent = gateway.create_intent(
        amount,
        metadata={
            "purpose": "provider_wallet_topup",
            "user_id": str(ctx_user_id),
            "method": method,
        },
    )
    txn = ProviderWalletTransaction(
        reference_id=generate_reference_id(session, PROVIDER_WALLET_TXN),
        wallet_id=wallet.id,
        user_id=ctx_user_id,
        type="credit",
        source="topup",
        amount=amount,
        balance_after=wallet.balance,
        status="pending",
        gateway_intent_id=intent["intent_id"],
        idempotency_key=idempotency_key,
        note=f"Provider wallet top-up ({method})",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    audit_emit(
        "provider_wallet.topup_initiated",
        trip_id=None,
        actor="provider",
        actor_id=str(ctx_user_id),
        payload={"txn_reference": txn.reference_id, "amount": amount},
    )
    return txn, intent["client_secret"]


def confirm_topup(session: Session, gateway_intent_id: str) -> bool:
    """Settle a pending provider top-up by its gateway intent id. Idempotent.
    Returns True when a matching provider top-up txn was found."""
    txn = session.exec(
        select(ProviderWalletTransaction)
        .where(
            ProviderWalletTransaction.gateway_intent_id == gateway_intent_id,
            ProviderWalletTransaction.source == "topup",
        )
        .with_for_update()
    ).first()
    if not txn:
        return False
    if txn.status == "success":
        return True
    if txn.status != "pending":
        return False

    wallet = session.exec(
        select(ProviderWallet)
        .where(ProviderWallet.id == txn.wallet_id)
        .with_for_update()
    ).first()
    cap = _get_config_float(
        session, PROVIDER_WALLET_MAX_BALANCE_KEY, DEFAULT_PROVIDER_WALLET_MAX_BALANCE
    )
    if wallet.balance + txn.amount > cap + _EPS:
        txn.status = "failed"
        txn.balance_after = wallet.balance
        txn.note = (txn.note or "") + f" — rejected: exceeds cap (₹{cap:.0f})"
        txn.updated_at = now_ist()
        session.add(txn)
        session.commit()
        return True

    wallet.balance = round(wallet.balance + txn.amount, 2)
    wallet.updated_at = now_ist()
    txn.status = "success"
    txn.balance_after = wallet.balance
    txn.updated_at = now_ist()
    session.add(wallet)
    session.add(txn)
    # The top-up money physically arrives in the merchant bank as provider-owed
    # (it backs the provider's own digital balance).
    from app.services import merchant_bank

    merchant_bank.credit_provider_owed(
        session,
        txn.amount,
        provider_user_id=wallet.user_id,
        provider_type=wallet.provider_type,
        note="Provider wallet top-up",
    )
    session.commit()
    audit_emit(
        "provider_wallet.topup_succeeded",
        trip_id=None,
        actor="system",
        actor_id="gateway_webhook",
        payload={"txn_reference": txn.reference_id, "amount": txn.amount},
    )
    return True


def simulate_topup_settlement(gateway_intent_id: str, amount: float) -> None:
    """Mock async settlement for a provider top-up (FastAPI BackgroundTask, own
    session). Routes a signed success event through the shared payment webhook
    handler, which falls back to ``confirm_topup`` when no Payment matches.
    Replace with the real gateway webhook once integrated."""
    from app.core.database import engine
    from app.modules.payments.service import handle_webhook

    _event, payload, signature = gateway.build_success_event(gateway_intent_id, amount)
    with Session(engine) as session:
        try:
            handle_webhook(session, payload, signature)
        except HTTPException:
            pass


def fail_topup(
    session: Session, gateway_intent_id: str, reason: Optional[str] = None
) -> bool:
    txn = session.exec(
        select(ProviderWalletTransaction)
        .where(
            ProviderWalletTransaction.gateway_intent_id == gateway_intent_id,
            ProviderWalletTransaction.source == "topup",
        )
        .with_for_update()
    ).first()
    if not txn:
        return False
    if txn.status == "success":
        return True
    if txn.status != "pending":
        return True
    txn.status = "failed"
    txn.note = (txn.note or "") + (f" — failed: {reason}" if reason else " — failed")
    txn.updated_at = now_ist()
    session.add(txn)
    session.commit()
    return True
