"""
Payout / cash-out execution.

Moves provider-owed money out of the merchant bank account to a provider's real
bank account via the payout partner, keeping the provider wallet, the merchant
ledger and the ``PayoutRecord`` table consistent:

  * Validate: wallet balance >= amount > 0  AND  amount <= merchant
    ``provider_owed_balance`` (never sweep user float).
  * Debit the wallet + append a merchant-bank debit row, then call the partner.
  * On bounce: re-credit the wallet (``reversal``) and reverse the merchant-bank
    entry; record the bounce on the ``PayoutRecord`` with its reason.

``execute_payout`` is idempotent on ``idempotency_key`` (the wallet/ledger moves
happen at most once per key).
"""

from __future__ import annotations

from typing import Dict, List, Optional
import uuid

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.models import (
    PayoutRecord,
    ProviderWallet,
    Driver,
    TowTruckDriver,
)
from app.modules.provider_wallet import service as pw_service
from app.modules.payout import partner as payout_partner
from app.services import merchant_bank
from app.services.audit_log import emit_event as audit_emit
from app.utils.id_generator import generate_reference_id, PAYOUT
from app.utils.time_utils import now_ist

_EPS = 1e-6

_BANK_MODELS = {"driver": Driver, "tow": TowTruckDriver}


def resolve_bank_details(
    session: Session, wallet: ProviderWallet
) -> Optional[Dict[str, str]]:
    model = _BANK_MODELS.get(wallet.provider_type)
    if not model:
        return None
    profile = session.exec(select(model).where(model.user_id == wallet.user_id)).first()
    if not profile or not (profile.bank_account_number and profile.bank_ifsc):
        return None
    return {
        "bank_name": profile.bank_name,
        "account_number": profile.bank_account_number,
        "ifsc": profile.bank_ifsc,
        "holder": profile.bank_account_holder_name,
    }


def _mask(account_number: Optional[str]) -> Optional[str]:
    if not account_number:
        return None
    s = str(account_number)
    return ("*" * max(0, len(s) - 4)) + s[-4:]


def execute_payout(
    session: Session,
    *,
    wallet: ProviderWallet,
    amount: float,
    kind: str,
    idempotency_key: Optional[str] = None,
) -> PayoutRecord:
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Payout amount must be positive")

    if idempotency_key:
        existing = session.exec(
            select(PayoutRecord).where(PayoutRecord.idempotency_key == idempotency_key)
        ).first()
        if existing:
            return existing

    freeze_reason = pw_service.driver_wallet_freeze_reason(
        session, wallet.user_id, wallet.provider_type
    )
    if freeze_reason:
        raise HTTPException(403, freeze_reason)

    if kind == "cashout":
        min_co = pw_service._get_config_float(
            session,
            pw_service.PROVIDER_CASHOUT_MIN_KEY,
            pw_service.DEFAULT_PROVIDER_CASHOUT_MIN,
        )
        if amount < min_co - _EPS:
            raise HTTPException(400, f"Minimum cash-out is ₹{min_co:.0f}")

    bank = resolve_bank_details(session, wallet)
    if not bank:
        raise HTTPException(400, "Bank details required before payout/cash-out")

    balances = merchant_bank.current_balances(session)
    if amount > balances["provider_owed_balance"] + _EPS:
        raise HTTPException(400, "Amount exceeds settled provider funds available")

    tag = "cashout" if kind == "cashout" else "payout"

    record = PayoutRecord(
        reference_id=generate_reference_id(session, PAYOUT),
        kind=kind,
        provider_user_id=wallet.user_id,
        provider_type=wallet.provider_type,
        amount=amount,
        status="initiated",
        bank_name=bank.get("bank_name"),
        account_number_masked=_mask(bank.get("account_number")),
        ifsc=bank.get("ifsc"),
        partner_provider=payout_partner.get_partner().provider,
        idempotency_key=idempotency_key,
    )
    session.add(record)
    session.flush()

    wallet_txn = pw_service.debit(
        session,
        wallet.user_id,
        wallet.provider_type,
        amount,
        source=tag,
        payout_reference=record.reference_id,
        note=f"{kind} to bank",
    )
    ledger = merchant_bank.debit_for_payout(
        session,
        amount,
        tag=tag,
        provider_user_id=wallet.user_id,
        provider_type=wallet.provider_type,
        payout_reference=record.reference_id,
        note=f"{kind} to provider bank",
    )
    record.wallet_txn_reference = wallet_txn.reference_id
    record.merchant_ledger_reference = ledger.reference_id

    result = payout_partner.get_partner().initiate_payout(
        amount, bank, idempotency_key=record.reference_id
    )

    if result.success:
        record.status = "success"
        record.partner_txn_id = result.partner_txn_id
        record.completed_at = now_ist()
    else:
        pw_service.reverse_debit(
            session,
            wallet.user_id,
            wallet.provider_type,
            amount,
            payout_reference=record.reference_id,
            note=f"{kind} bounce reversal",
        )
        merchant_bank.reverse_payout(
            session,
            amount,
            tag=tag,
            provider_user_id=wallet.user_id,
            provider_type=wallet.provider_type,
            payout_reference=record.reference_id,
        )
        record.status = "bounced"
        record.failure_reason = result.failure_reason

    record.updated_at = now_ist()
    session.add(record)
    session.commit()
    session.refresh(record)

    audit_emit(
        f"payout.{record.status}",
        trip_id=None,
        actor="scheduler" if kind == "auto_payout" else "provider",
        actor_id=str(wallet.user_id),
        payload={
            "payout_reference": record.reference_id,
            "kind": kind,
            "amount": amount,
            "status": record.status,
        },
        severity="warning" if record.status == "bounced" else "info",
    )
    return record


def _record_failed_sweep(
    session: Session, wallet: ProviderWallet, amount: float, reason: str
) -> Optional[PayoutRecord]:
    key = f"sweep:{wallet.reference_id}:{now_ist().date().isoformat()}"
    existing = session.exec(
        select(PayoutRecord).where(PayoutRecord.idempotency_key == key)
    ).first()
    if existing:
        return existing
    try:
        record = PayoutRecord(
            reference_id=generate_reference_id(session, PAYOUT),
            kind="auto_payout",
            provider_user_id=wallet.user_id,
            provider_type=wallet.provider_type,
            amount=round(amount, 2),
            status="failed",
            failure_reason=reason,
            partner_provider=payout_partner.get_partner().provider,
            idempotency_key=key,
            completed_at=now_ist(),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
    except Exception:
        session.rollback()
        return None
    audit_emit(
        "payout.failed",
        trip_id=None,
        actor="scheduler",
        actor_id=str(wallet.user_id),
        payload={
            "payout_reference": record.reference_id,
            "kind": "auto_payout",
            "amount": record.amount,
            "status": "failed",
            "reason": reason,
        },
        severity="warning",
    )
    return record


def sweep_all_providers(session: Session) -> List[str]:
    wallets = session.exec(
        select(ProviderWallet).where(ProviderWallet.balance > 0)
    ).all()
    refs: List[str] = []
    for w in wallets:
        amount = round(w.balance, 2)
        if amount <= 0:
            continue
        if pw_service.driver_wallet_freeze_reason(session, w.user_id, w.provider_type):
            audit_emit(
                "payout.sweep_skipped",
                trip_id=None,
                actor="scheduler",
                payload={
                    "wallet_reference": w.reference_id,
                    "reason": "wallet_frozen_active_trip",
                },
                severity="info",
            )
            continue
        if not resolve_bank_details(session, w):
            if w.provider_type in _BANK_MODELS:
                _record_failed_sweep(
                    session,
                    w,
                    amount,
                    "Missing or incomplete bank details (account number / IFSC required)",
                )
            continue
        try:
            rec = execute_payout(
                session,
                wallet=w,
                amount=amount,
                kind="auto_payout",
                idempotency_key=f"sweep:{w.reference_id}:{now_ist().date().isoformat()}",
            )
            refs.append(rec.reference_id)
        except Exception:
            session.rollback()
            audit_emit(
                "payout.sweep_skipped",
                trip_id=None,
                actor="scheduler",
                payload={"wallet_reference": w.reference_id},
                severity="warning",
            )
    return refs
