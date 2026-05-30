"""
Prepaid wallet logic — balances, transactions, top-ups, credits and debits.

Design rules:
  * Every balance mutation happens under a ``SELECT ... FOR UPDATE`` row lock so
    concurrent credits/debits can't race (no lost updates, never goes negative).
  * Mutating helpers (``credit`` / ``debit_for_payment``) DO NOT commit — the
    caller owns the transaction so a wallet debit and the payment it pays for
    commit atomically together. Top-up settlement and admin credit commit
    themselves because they are standalone operations.
  * RBI non-KYC PPI style limits (min/max top-up, max balance) are read from
    ``SystemConfig`` so they can be tuned without a deploy.
"""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from app.core.models import Wallet, WalletTransaction, SystemConfig, User, Payment
from app.modules.payments import gateway
from app.services.audit_log import emit_event as audit_emit
from app.utils.id_generator import generate_reference_id, WALLET_TXN
from app.utils.time_utils import now_ist

# --- SystemConfig keys + defaults (admin-tunable via /admin/system-config) ---
WALLET_MIN_TOPUP_KEY = "wallet_min_topup"
WALLET_MAX_TOPUP_KEY = "wallet_max_topup"
WALLET_MAX_BALANCE_KEY = "wallet_max_balance"
DEFAULT_WALLET_MIN_TOPUP = 1.0
DEFAULT_WALLET_MAX_TOPUP = 50000.0
DEFAULT_WALLET_MAX_BALANCE = 10000.0  # RBI non-KYC PPI ceiling

_EPS = 1e-6  # float tolerance for money comparisons


def _get_config_float(session: Session, key: str, default: float) -> float:
    cfg = session.get(SystemConfig, key)
    if cfg and cfg.value:
        try:
            return float(cfg.value)
        except (TypeError, ValueError):
            pass
    return default


# --- wallet lifecycle ---------------------------------------------------------
def get_or_create_wallet(session: Session, user_id) -> Wallet:
    """Return the user's wallet, lazily creating it on first use.

    Create races (two requests for a first-time user) are absorbed via a
    SAVEPOINT + unique(user_id) constraint, then re-fetched."""
    wallet = session.exec(select(Wallet).where(Wallet.user_id == user_id)).first()
    if wallet:
        return wallet
    try:
        with session.begin_nested():
            wallet = Wallet(user_id=user_id)
            session.add(wallet)
            session.flush()
    except IntegrityError:
        wallet = session.exec(select(Wallet).where(Wallet.user_id == user_id)).first()
        if wallet is None:
            raise
    return wallet


def _lock_wallet(session: Session, user_id) -> Wallet:
    """Ensure the wallet exists, then re-select it under a row lock."""
    get_or_create_wallet(session, user_id)
    return session.exec(
        select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    ).first()


def _pending_topup_total(session: Session, wallet_id: int) -> float:
    rows = session.exec(
        select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet_id,
            WalletTransaction.source == "topup",
            WalletTransaction.status == "pending",
        )
    ).all()
    return round(sum(r.amount for r in rows), 2)


# --- credits / debits (no commit — caller owns the transaction) --------------
def credit(
    session: Session,
    user: User,
    amount: float,
    source: str,
    *,
    note: Optional[str] = None,
    payment_reference: Optional[str] = None,
    related_service_type: Optional[str] = None,
    related_service_reference_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    enforce_cap: bool = True,
    allow_inactive: bool = False,
) -> WalletTransaction:
    """Add money to the wallet (refund / admin / promo). Returns the txn."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Credit amount must be positive")

    wallet = _lock_wallet(session, user.id)
    # Refunds (allow_inactive) still land even on a frozen wallet — the money is
    # owed back to the user regardless of freeze state.
    if not wallet.is_active and not allow_inactive:
        raise HTTPException(403, "Wallet is inactive")

    if enforce_cap:
        cap = _get_config_float(
            session, WALLET_MAX_BALANCE_KEY, DEFAULT_WALLET_MAX_BALANCE
        )
        if wallet.balance + amount > cap + _EPS:
            raise HTTPException(
                400, f"This credit would exceed the wallet balance cap (₹{cap:.0f})"
            )

    wallet.balance = round(wallet.balance + amount, 2)
    wallet.updated_at = now_ist()
    session.add(wallet)

    txn = WalletTransaction(
        reference_id=generate_reference_id(session, WALLET_TXN),
        wallet_id=wallet.id,
        user_id=user.id,
        type="credit",
        source=source,
        amount=amount,
        balance_after=wallet.balance,
        status="success",
        note=note,
        payment_reference=payment_reference,
        related_service_type=related_service_type,
        related_service_reference_id=related_service_reference_id,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    session.flush()
    return txn


def debit_for_payment(
    session: Session, user: User, amount: float, payment: Payment
) -> WalletTransaction:
    """Debit the wallet for a booking payment. Raises 400 on insufficient funds.

    Runs inside the payment's transaction so the debit + payment settle as one."""
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Debit amount must be positive")

    wallet = _lock_wallet(session, user.id)
    if not wallet.is_active:
        raise HTTPException(403, "Wallet is inactive")
    if wallet.balance + _EPS < amount:
        raise HTTPException(400, "Insufficient wallet balance")

    wallet.balance = round(wallet.balance - amount, 2)
    wallet.updated_at = now_ist()
    session.add(wallet)

    txn = WalletTransaction(
        reference_id=generate_reference_id(session, WALLET_TXN),
        wallet_id=wallet.id,
        user_id=user.id,
        type="debit",
        source="payment",
        amount=amount,
        balance_after=wallet.balance,
        status="success",
        payment_reference=payment.reference_id,
        related_service_type=payment.service_type,
        related_service_reference_id=payment.service_reference_id,
    )
    session.add(txn)
    session.flush()
    return txn


# --- top-up (via gateway) -----------------------------------------------------
def create_topup_intent(
    session: Session,
    user: User,
    amount: float,
    idempotency_key: Optional[str] = None,
) -> Tuple[WalletTransaction, Optional[str]]:
    """Start a wallet top-up: enforce limits, create a gateway intent and a
    PENDING credit txn. Settlement arrives on the gateway webhook.

    DB-level idempotency: replaying the same ``idempotency_key`` returns the
    existing top-up instead of creating a second gateway intent (the route's
    Redis guard handles client_secret replay within its TTL)."""
    if idempotency_key:
        existing = session.exec(
            select(WalletTransaction).where(
                WalletTransaction.idempotency_key == idempotency_key,
                WalletTransaction.source == "topup",
            )
        ).first()
        if existing:
            return existing, None

    amount = round(float(amount), 2)
    min_t = _get_config_float(session, WALLET_MIN_TOPUP_KEY, DEFAULT_WALLET_MIN_TOPUP)
    max_t = _get_config_float(session, WALLET_MAX_TOPUP_KEY, DEFAULT_WALLET_MAX_TOPUP)
    cap = _get_config_float(session, WALLET_MAX_BALANCE_KEY, DEFAULT_WALLET_MAX_BALANCE)

    if amount < min_t - _EPS:
        raise HTTPException(400, f"Minimum top-up is ₹{min_t:.0f}")
    if amount > max_t + _EPS:
        raise HTTPException(400, f"Maximum top-up is ₹{max_t:.0f}")

    wallet = get_or_create_wallet(session, user.id)
    if not wallet.is_active:
        raise HTTPException(403, "Wallet is inactive")

    # Pending top-ups also count toward the cap to prevent over-funding races.
    pending = _pending_topup_total(session, wallet.id)
    if wallet.balance + pending + amount > cap + _EPS:
        raise HTTPException(
            400, f"This top-up would exceed your wallet balance cap (₹{cap:.0f})"
        )

    intent = gateway.create_intent(
        amount, metadata={"purpose": "wallet_topup", "user_id": str(user.id)}
    )
    txn = WalletTransaction(
        reference_id=generate_reference_id(session, WALLET_TXN),
        wallet_id=wallet.id,
        user_id=user.id,
        type="credit",
        source="topup",
        amount=amount,
        balance_after=wallet.balance,  # unchanged until settled
        status="pending",
        gateway_intent_id=intent["intent_id"],
        idempotency_key=idempotency_key,
        note="Wallet top-up",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)

    audit_emit(
        "wallet.topup_initiated",
        trip_id=None,
        actor="user",
        actor_id=str(user.id),
        payload={"txn_reference": txn.reference_id, "amount": amount},
    )
    return txn, intent["client_secret"]


def confirm_topup(session: Session, gateway_intent_id: str) -> bool:
    """Settle a pending top-up identified by its gateway intent id.

    Idempotent: a second delivery for an already-settled top-up is a no-op.
    Returns True when a matching top-up txn was found (settled or already so)."""
    txn = session.exec(
        select(WalletTransaction)
        .where(
            WalletTransaction.gateway_intent_id == gateway_intent_id,
            WalletTransaction.source == "topup",
        )
        .with_for_update()
    ).first()
    if not txn:
        return False
    if txn.status == "success":
        return True  # already settled
    if txn.status != "pending":
        return False

    wallet = session.exec(
        select(Wallet).where(Wallet.id == txn.wallet_id).with_for_update()
    ).first()
    wallet.balance = round(wallet.balance + txn.amount, 2)
    wallet.updated_at = now_ist()
    txn.status = "success"
    txn.balance_after = wallet.balance
    txn.updated_at = now_ist()
    session.add(wallet)
    session.add(txn)
    session.commit()

    audit_emit(
        "wallet.topup_succeeded",
        trip_id=None,
        actor="system",
        actor_id="gateway_webhook",
        payload={"txn_reference": txn.reference_id, "amount": txn.amount},
    )
    return True


def simulate_topup_settlement(gateway_intent_id: str, amount: float) -> None:
    """Mock async settlement for a top-up (FastAPI BackgroundTask, own session).

    Builds a signed success event and routes it through the shared payment
    webhook handler, which falls back to confirm_topup when no Payment matches.
    Replace with the real gateway's out-of-band webhook once integrated."""
    from app.core.database import engine
    from app.modules.payments.service import handle_webhook

    _event, payload, signature = gateway.build_success_event(gateway_intent_id, amount)
    with Session(engine) as session:
        try:
            handle_webhook(session, payload, signature)
        except HTTPException:
            pass
