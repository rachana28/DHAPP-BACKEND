"""
Mock payment gateway — mimics Stripe/Razorpay shape so the rest of the payment
module is provider-agnostic. Swap these four functions for a real client
(create order/intent, verify webhook signature) without touching service.py.

Signing uses HMAC-SHA256 over the canonical JSON payload, exactly like the
real providers' webhook verification, so /payments/webhook validation code is
production-shaped from day one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Any, Dict, Optional, Tuple
from app.utils import card_utils

# In production set PAYMENT_GATEWAY_WEBHOOK_SECRET to the provider's signing key.
_SECRET = os.environ.get("PAYMENT_GATEWAY_WEBHOOK_SECRET", "mock_dev_secret")

PROVIDER = "mock"


def create_intent(
    amount: float, currency: str = "INR", metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Create a payment intent/order. Returns the gateway handle + client secret."""
    intent_id = f"pi_mock_{uuid.uuid4().hex[:16]}"
    return {
        "intent_id": intent_id,
        "client_secret": f"{intent_id}_secret_{uuid.uuid4().hex[:12]}",
        "amount": round(amount, 2),
        "currency": currency,
        "status": "requires_confirmation",
        "metadata": metadata or {},
    }


def tokenize_card(
    card_number: str,
    exp_month: int,
    exp_year: int,
    cvv: str,
    holder_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Mock tokenization vault — mimics a real gateway's card-vault call.

    Returns ONLY the safe handle (token + fingerprint + last4 + brand). The raw
    PAN and CVV are consumed here and never returned, stored, or logged.

    The ``fingerprint`` is a stable, non-reversible hash of the PAN so the same
    physical card can be de-duplicated without storing the number. In real
    production, tokenization should happen client-side via the provider SDK so
    the PAN never reaches our server; this mirrors that contract for the mock.
    """

    digits = "".join(ch for ch in card_number if ch.isdigit())
    fingerprint = hashlib.sha256(f"{_SECRET}:{digits}".encode()).hexdigest()[:24]
    return {
        "card_token": f"tok_mock_{uuid.uuid4().hex[:24]}",
        "fingerprint": fingerprint,
        "last4": card_utils.last4(digits),
        "brand": card_utils.detect_brand(digits),
    }


def sign(payload: str) -> str:
    return hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: str, signature: Optional[str]) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(payload), signature)


def build_success_event(
    intent_id: str, amount: float
) -> Tuple[Dict[str, Any], str, str]:
    """Construct a signed ``payment_intent.succeeded`` event (the mock 'webhook').

    Returns ``(event, canonical_payload, signature)``. The real gateway sends
    the payload + signature header; we reproduce both so handle_webhook runs the
    same verification path.
    """
    event = {
        "id": f"evt_mock_{uuid.uuid4().hex[:12]}",
        "type": "payment_intent.succeeded",
        "data": {
            "intent_id": intent_id,
            "gateway_transaction_id": f"txn_mock_{uuid.uuid4().hex[:16]}",
            "amount": round(amount, 2),
            "status": "succeeded",
        },
    }
    payload = json.dumps(event, separators=(",", ":"), sort_keys=True)
    return event, payload, sign(payload)
