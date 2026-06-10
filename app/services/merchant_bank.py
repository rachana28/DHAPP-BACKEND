"""
Merchant-bank ledger — the single physical-money source of truth.

Every rupee that physically reaches (or leaves) the platform merchant bank
account appends exactly one ``MerchantBankLedger`` row. Two sub-balances are
tracked alongside the running balance so the payout sweep can move ONLY money
owed to providers and never touch user wallet float:

  * ``provider_owed_balance`` — backs providers' digital wallet balances.
  * ``user_float_balance``    — backs users' prepaid wallet balances.

Invariant: ``running_balance == provider_owed_balance + user_float_balance``.

All helpers run inside the caller's open transaction (no commit here) and use a
row lock on the latest row to serialize concurrent appends.
"""

from __future__ import annotations

from typing import Optional
import uuid

from sqlmodel import Session, select

from app.core.models import MerchantBankLedger
from app.utils.id_generator import generate_reference_id, MERCHANT_LEDGER


def _append(
    session: Session,
    *,
    entry_type: str,
    tag: str,
    amount: float,
    d_running: float,
    d_pob: float,
    d_ufb: float,
    provider_user_id: Optional[uuid.UUID] = None,
    provider_type: Optional[str] = None,
    service_type: Optional[str] = None,
    service_reference_id: Optional[str] = None,
    payment_reference: Optional[str] = None,
    payout_reference: Optional[str] = None,
    note: Optional[str] = None,
) -> MerchantBankLedger:
    amount = round(float(amount), 2)
    latest = session.exec(
        select(MerchantBankLedger)
        .order_by(MerchantBankLedger.id.desc())
        .with_for_update()
    ).first()
    rb = latest.running_balance if latest else 0.0
    pob = latest.provider_owed_balance if latest else 0.0
    ufb = latest.user_float_balance if latest else 0.0

    row = MerchantBankLedger(
        reference_id=generate_reference_id(session, MERCHANT_LEDGER),
        entry_type=entry_type,
        tag=tag,
        amount=amount,
        running_balance=round(rb + d_running, 2),
        provider_owed_balance=round(pob + d_pob, 2),
        user_float_balance=round(ufb + d_ufb, 2),
        provider_user_id=provider_user_id,
        provider_type=provider_type,
        service_type=service_type,
        service_reference_id=service_reference_id,
        payment_reference=payment_reference,
        payout_reference=payout_reference,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


def credit_provider_owed(session: Session, amount: float, **meta) -> MerchantBankLedger:
    """Online (card/UPI) booking payment landed physically — owed to a provider."""
    a = round(float(amount), 2)
    return _append(
        session,
        entry_type="credit",
        tag="provider_owed",
        amount=a,
        d_running=a,
        d_pob=a,
        d_ufb=0.0,
        **meta,
    )


def credit_user_float(session: Session, amount: float, **meta) -> MerchantBankLedger:
    """User topped up their prepaid wallet — physical money is user float."""
    a = round(float(amount), 2)
    return _append(
        session,
        entry_type="credit",
        tag="user_float",
        amount=a,
        d_running=a,
        d_pob=0.0,
        d_ufb=a,
        **meta,
    )


def reclassify_to_provider_owed(
    session: Session, amount: float, **meta
) -> MerchantBankLedger:
    """User paid a booking FROM their wallet: no physical move, but the money is
    now owed to the provider instead of being user float."""
    a = round(float(amount), 2)
    return _append(
        session,
        entry_type="reclassify",
        tag="provider_owed",
        amount=a,
        d_running=0.0,
        d_pob=a,
        d_ufb=-a,
        note=(meta.pop("note", None) or "Reclassified user float -> provider owed"),
        **meta,
    )


def reclassify_to_user_float(
    session: Session, amount: float, **meta
) -> MerchantBankLedger:
    """Refund of a wallet-paid booking: the money is owed back to the user as
    float again (no physical move)."""
    a = round(float(amount), 2)
    return _append(
        session,
        entry_type="reclassify",
        tag="user_float",
        amount=a,
        d_running=0.0,
        d_pob=-a,
        d_ufb=a,
        note=(meta.pop("note", None) or "Reclassified provider owed -> user float"),
        **meta,
    )


def debit_provider_owed_refund(
    session: Session, amount: float, **meta
) -> MerchantBankLedger:
    """Gateway/UPI refund: money physically leaves the merchant bank back to the
    user's original source — reduce provider-owed by the refunded amount."""
    a = round(float(amount), 2)
    return _append(
        session,
        entry_type="debit",
        tag="refund",
        amount=a,
        d_running=-a,
        d_pob=-a,
        d_ufb=0.0,
        note=(meta.pop("note", None) or "Online payment refund"),
        **meta,
    )


def debit_for_payout(session: Session, amount: float, **meta) -> MerchantBankLedger:
    """Money physically left the merchant bank to a provider's real bank."""
    a = round(float(amount), 2)
    tag = meta.pop("tag", "payout")
    return _append(
        session,
        entry_type="debit",
        tag=tag,
        amount=a,
        d_running=-a,
        d_pob=-a,
        d_ufb=0.0,
        **meta,
    )


def reverse_payout(session: Session, amount: float, **meta) -> MerchantBankLedger:
    """A payout bounced — undo the physical debit (money never actually left)."""
    a = round(float(amount), 2)
    tag = meta.pop("tag", "payout")
    return _append(
        session,
        entry_type="credit",
        tag=tag,
        amount=a,
        d_running=a,
        d_pob=a,
        d_ufb=0.0,
        note=(meta.pop("note", None) or "Payout bounce reversal"),
        **meta,
    )


def current_balances(session: Session) -> dict:
    latest = session.exec(
        select(MerchantBankLedger).order_by(MerchantBankLedger.id.desc())
    ).first()
    return {
        "running_balance": latest.running_balance if latest else 0.0,
        "provider_owed_balance": latest.provider_owed_balance if latest else 0.0,
        "user_float_balance": latest.user_float_balance if latest else 0.0,
    }
