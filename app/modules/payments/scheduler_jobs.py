"""Background jobs for the centralized payment ledger.

``expire_stale_payment_intents_scheduler``: a gateway/direct intent that never
settles — the webhook never arrives, or the provider never confirms a
cash/upi_direct collection — would otherwise sit in ``created``/``pending``
forever, so clients keep polling a dead intent and the ledger never reconciles.
After a configurable TTL (``payment_intent_ttl_minutes`` in SystemConfig,
default 120) we move such intents to ``cancelled``. Succeeded / refunded /
failed intents are never touched. Pending wallet top-ups that never settled are
expired in parallel via ``wallet_service.fail_topup``.

The TTL default is intentionally generous so a slow but legitimate
cash/upi_direct collection is not killed mid-flight; admins can tune it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import Session, select

from app.core.database import engine
from app.core.models import Payment, SystemConfig, WalletTransaction
from app.modules.wallet import service as wallet_service
from app.services.audit_log import emit_event as audit_emit
from app.utils.time_utils import now_ist

logger = logging.getLogger("dhapp.payments.scheduler")

PAYMENT_INTENT_TTL_MIN_KEY = "payment_intent_ttl_minutes"
DEFAULT_PAYMENT_INTENT_TTL_MIN = 120  # admin-tunable via /admin/system-config


def _ttl_minutes(session: Session) -> int:
    cfg = session.get(SystemConfig, PAYMENT_INTENT_TTL_MIN_KEY)
    if cfg and cfg.value:
        try:
            return max(1, int(float(cfg.value)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_PAYMENT_INTENT_TTL_MIN


async def expire_stale_payment_intents_scheduler() -> None:
    try:
        with Session(engine) as session:
            cutoff = now_ist() - timedelta(minutes=_ttl_minutes(session))

            stale = session.exec(
                select(Payment).where(
                    Payment.status.in_(["created", "pending"]),
                    Payment.created_at < cutoff,
                )
            ).all()
            cancelled = 0
            for p in stale:
                # Re-check under the same session in case a webhook settled it
                # between the scan and now — never cancel a settled charge.
                if p.status not in ("created", "pending"):
                    continue
                p.status = "cancelled"
                p.updated_at = now_ist()
                p.extra = {**(p.extra or {}), "cancel_reason": "intent_expired"}
                session.add(p)
                audit_emit(
                    "payment.cancelled",
                    trip_id=None,
                    actor="system",
                    actor_id="scheduler",
                    payload={
                        "payment_reference": p.reference_id,
                        "reason": "intent_expired",
                    },
                )
                cancelled += 1
            if cancelled:
                session.commit()
                logger.info(
                    f"Expired {cancelled} stale payment intent(s) to cancelled."
                )

            # Expire pending wallet top-ups that never settled (fail_topup commits).
            stale_topups = session.exec(
                select(WalletTransaction).where(
                    WalletTransaction.source == "topup",
                    WalletTransaction.status == "pending",
                    WalletTransaction.created_at < cutoff,
                )
            ).all()
            for t in stale_topups:
                if t.gateway_intent_id:
                    wallet_service.fail_topup(
                        session, t.gateway_intent_id, reason="intent_expired"
                    )
    except Exception as e:  # never let a cleanup tick crash the scheduler loop
        logger.error(f"Stale payment-intent expiry scheduler failed: {e}")
