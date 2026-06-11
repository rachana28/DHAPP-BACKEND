"""
Payout bank-partner abstraction (swappable, mirrors payments/gateway.py).

The platform sweeps provider-owed money out of its merchant bank account to each
provider's registered bank account through a *payout partner* (RazorpayX,
Cashfree Payouts, etc.). Only the mock implementation ships today; a real
partner is slotted in later by implementing :class:`PayoutPartner` and pointing
:func:`get_partner` at it — no caller changes required.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class PayoutResult:
    success: bool
    partner_txn_id: Optional[str] = None
    failure_reason: Optional[str] = None


class PayoutPartner:
    provider: str = "abstract"

    def initiate_payout(
        self, amount: float, bank_details: Dict[str, str], *, idempotency_key: str
    ) -> PayoutResult:
        raise NotImplementedError


class MockPayoutPartner(PayoutPartner):
    provider = "mock"

    def initiate_payout(
        self, amount: float, bank_details: Dict[str, str], *, idempotency_key: str
    ) -> PayoutResult:
        force_bounce = os.environ.get("PAYOUT_FORCE_BOUNCE") == "1"
        acct = str(bank_details.get("account_number") or "")
        if force_bounce or acct.endswith("0000"):
            return PayoutResult(
                success=False,
                failure_reason="Mock partner: account verification failed (bounce)",
            )
        return PayoutResult(
            success=True, partner_txn_id=f"MOCKPO_{uuid.uuid4().hex[:16]}"
        )


_PARTNER: Optional[PayoutPartner] = None


def get_partner() -> PayoutPartner:
    global _PARTNER
    if _PARTNER is None:
        _PARTNER = MockPayoutPartner()
    return _PARTNER
