"""
Centralized payment orchestration for tow / mechanic / service-center bookings.

Channels:
  - ``platform``   : money held by the platform via the gateway. /create-intent
                     returns a client secret; settlement arrives async on the
                     gateway webhook (mocked by simulate_webhook_delivery).
  - ``cash`` / ``upi_direct`` : collected directly by the provider. No gateway
                     money movement; the assigned provider confirms via
                     /payments/{ref}/mark-paid.

On success the linked booking's ``payment_status`` is flipped to ``paid``
(_sync_booking). Trip flows are intentionally NOT routed here yet — they keep
the legacy PaymentTransaction/TripBill/TripSettlement pipeline.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.models import (
    Payment,
    PaymentIntentCreate,
    SavedCard,
    TowTrip,
    MechanicTrip,
    ServiceRequest,
    ServiceCenter,
    TowTruckDriver,
    Mechanic,
    User,
)
from app.modules.wallet import service as wallet_service
from app.modules.payments import gateway
from app.services.audit_log import emit_event as audit_emit
from app.utils.id_generator import (
    generate_reference_id,
    get_by_reference,
    PAYMENT,
    TOW_TRIP,
    MECHANIC_TRIP,
    SERVICE_REQUEST,
)
from app.utils.time_utils import now_ist

DIRECT_CHANNELS = {"cash", "upi_direct"}

ALL_CHANNELS = {"platform", "wallet"} | DIRECT_CHANNELS

# service_type -> how to resolve the booking, its amount, and its provider.
_SERVICE_MAP: Dict[str, Dict[str, Any]] = {
    "tow": {
        "model": TowTrip,
        "entity": TOW_TRIP,
        "amount_attrs": ("fare",),
        "provider_attr": "tow_truck_driver_id",
    },
    "mechanic": {
        "model": MechanicTrip,
        "entity": MECHANIC_TRIP,
        "amount_attrs": ("fare",),
        "provider_attr": "mechanic_id",
    },
    "service_center": {
        "model": ServiceRequest,
        "entity": SERVICE_REQUEST,
        "amount_attrs": ("final_price", "price_at_booking"),
        "provider_attr": None,  # collected by the center, not a single driver
    },
}


def _cfg(service_type: str) -> Dict[str, Any]:
    cfg = _SERVICE_MAP.get(service_type)
    if not cfg:
        raise HTTPException(400, f"Unsupported service_type '{service_type}'")
    return cfg


def _resolve_booking(session: Session, service_type: str, service_reference_id: str):
    cfg = _cfg(service_type)
    booking = get_by_reference(session, cfg["model"], service_reference_id)
    if not booking:
        raise HTTPException(404, "Booking not found for payment")
    return booking


def _derive_amount(booking, cfg: Dict[str, Any]) -> Optional[float]:
    for attr in cfg["amount_attrs"]:
        val = getattr(booking, attr, None)
        if val:
            return float(val)
    return None


def _validate_card_reference(session: Session, user: User, card_reference: str) -> str:
    """Ensure a saved-card reference belongs to the payer and is active."""
    card = get_by_reference(session, SavedCard, card_reference)
    if not card or card.user_id != user.id or not card.is_active:
        raise HTTPException(400, "Invalid card reference")
    return card_reference


def create_payment_intent(
    session: Session,
    user: User,
    data: PaymentIntentCreate,
    idempotency_key: Optional[str] = None,
) -> Tuple[Payment, Optional[str]]:
    """Create (or replay) a payment for a booking. Returns (payment, client_secret)."""
    if data.channel not in ALL_CHANNELS:
        raise HTTPException(400, f"channel must be one of {sorted(ALL_CHANNELS)}")

    cfg = _cfg(data.service_type)
    booking = _resolve_booking(session, data.service_type, data.service_reference_id)
    if booking.user_id != user.id:
        raise HTTPException(403, "Not authorized to pay for this booking")

    # DB-level idempotency: same key returns the existing payment untouched.
    if idempotency_key:
        existing = session.exec(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        ).first()
        if existing:
            return existing, (existing.extra or {}).get("client_secret")

    amount = data.amount if data.amount is not None else _derive_amount(booking, cfg)
    if not amount or amount <= 0:
        raise HTTPException(400, "Could not determine a positive payment amount")
    amount = round(float(amount), 2)

    provider_attr = cfg["provider_attr"]
    payee_driver_id = getattr(booking, provider_attr, None) if provider_attr else None
    payee_type = "platform" if data.channel in ("platform", "wallet") else "provider"

    payment = Payment(
        reference_id=generate_reference_id(session, PAYMENT),
        service_type=data.service_type,
        service_reference_id=data.service_reference_id,
        service_id=booking.id,
        user_id=user.id,
        payee_type=payee_type,
        payee_driver_id=payee_driver_id,
        amount=amount,
        channel=data.channel,
        idempotency_key=idempotency_key,
    )

    client_secret: Optional[str] = None
    if data.channel == "platform":
        intent = gateway.create_intent(
            amount,
            metadata={
                "payment_reference": payment.reference_id,
                "service_type": data.service_type,
                "service_reference_id": data.service_reference_id,
            },
        )
        payment.gateway_provider = gateway.PROVIDER
        payment.gateway_intent_id = intent["intent_id"]
        payment.status = "pending"
        client_secret = intent["client_secret"]
        extra: Dict[str, Any] = {"client_secret": client_secret}
        card_ref = getattr(data, "card_reference_id", None)
        if card_ref:
            extra["card_reference"] = _validate_card_reference(session, user, card_ref)
        payment.extra = extra
    elif data.channel == "wallet":
        if getattr(booking, "payment_status", None) == "paid":
            raise HTTPException(400, "Booking is already paid")

        wallet_amount = _derive_amount(booking, cfg)
        if not wallet_amount or wallet_amount <= 0:
            raise HTTPException(400, "Could not determine a positive payment amount")
        wallet_amount = round(float(wallet_amount), 2)
        payment.amount = wallet_amount

        wallet_txn = wallet_service.debit_for_payment(
            session, user, wallet_amount, payment
        )
        payment.status = "succeeded"
        payment.completed_at = now_ist()
        payment.extra = {"wallet_txn_reference": wallet_txn.reference_id}
        _sync_booking(session, payment)
    else:
        # Direct cash/UPI: awaits provider confirmation via mark-paid.
        payment.status = "created"

    session.add(payment)
    session.commit()
    session.refresh(payment)

    if data.channel == "wallet":
        audit_emit(
            "payment.wallet_debited",
            trip_id=None,
            actor="user",
            actor_id=str(user.id),
            payload={
                "payment_reference": payment.reference_id,
                "service_type": data.service_type,
                "service_reference_id": data.service_reference_id,
                "amount": payment.amount,
            },
        )

    audit_emit(
        "payment.intent_created",
        trip_id=None,
        actor="user",
        actor_id=str(user.id),
        payload={
            "payment_reference": payment.reference_id,
            "service_type": data.service_type,
            "service_reference_id": data.service_reference_id,
            "channel": data.channel,
            "amount": amount,
        },
    )
    return payment, client_secret


def handle_webhook(
    session: Session, raw_payload: str, signature: Optional[str]
) -> Dict[str, str]:
    """Process a gateway webhook. Verifies signature, marks the payment succeeded,
    and syncs the booking. Idempotent on the intent id."""
    if not gateway.verify_signature(raw_payload, signature):
        raise HTTPException(400, "Invalid webhook signature")

    import json

    event = json.loads(raw_payload)
    if event.get("type") != "payment_intent.succeeded":
        return {"status": "ignored"}

    info = event.get("data", {})
    intent_id = info.get("intent_id")
    payment = session.exec(
        select(Payment).where(Payment.gateway_intent_id == intent_id).with_for_update()
    ).first()
    if not payment:
        if wallet_service.confirm_topup(session, intent_id):
            return {"status": "ok", "kind": "wallet_topup"}
        raise HTTPException(404, "Unknown payment intent")

    if payment.status == "succeeded":
        return {"status": "already_processed"}

    payment.status = "succeeded"
    payment.gateway_transaction_id = info.get("gateway_transaction_id")
    payment.gateway_signature = signature
    payment.completed_at = now_ist()
    payment.updated_at = now_ist()
    session.add(payment)
    _sync_booking(session, payment)
    session.commit()

    audit_emit(
        "payment.succeeded",
        trip_id=None,
        actor="system",
        actor_id="gateway_webhook",
        payload={
            "payment_reference": payment.reference_id,
            "channel": payment.channel,
            "amount": payment.amount,
        },
    )
    return {"status": "ok"}


def simulate_webhook_delivery(payment_reference: str) -> None:
    """Mock async settlement: deliver a signed success webhook for a platform
    payment. Runs as a FastAPI BackgroundTask with its own session. Replace with
    the real gateway's out-of-band webhook once integrated."""
    from app.core.database import engine

    with Session(engine) as session:
        payment = get_by_reference(session, Payment, payment_reference)
        if not payment or not payment.gateway_intent_id:
            return
        _event, payload, signature = gateway.build_success_event(
            payment.gateway_intent_id, payment.amount
        )
        try:
            handle_webhook(session, payload, signature)
        except HTTPException:
            pass


def mark_direct_paid(session: Session, payment: Payment, caller: User) -> Payment:
    """Provider confirms they collected cash/UPI directly. Only the assigned
    provider for the booking may do this."""
    if payment.channel not in DIRECT_CHANNELS:
        raise HTTPException(
            400, "Only cash / upi_direct payments can be marked paid manually"
        )
    if payment.status == "succeeded":
        return payment

    _authorize_provider(session, payment, caller)

    payment.status = "succeeded"
    payment.gateway_transaction_id = f"DIRECT_{payment.channel}_{payment.reference_id}"
    payment.completed_at = now_ist()
    payment.updated_at = now_ist()
    session.add(payment)
    _sync_booking(session, payment)
    session.commit()
    session.refresh(payment)

    audit_emit(
        "payment.marked_paid_direct",
        trip_id=None,
        actor="driver",
        actor_id=str(caller.id),
        payload={
            "payment_reference": payment.reference_id,
            "channel": payment.channel,
            "amount": payment.amount,
        },
    )
    return payment


def _authorize_provider(session: Session, payment: Payment, caller: User) -> None:
    """Raise 403 unless ``caller`` is the provider assigned to the payment's booking."""
    st = payment.service_type
    if st == "service_center":
        center = session.exec(
            select(ServiceCenter).where(ServiceCenter.user_id == caller.id)
        ).first()
        booking = session.get(ServiceRequest, payment.service_id)
        if not center or not booking or booking.service_center_id != center.id:
            raise HTTPException(
                403, "Only the assigned service center can confirm this"
            )
        return

    if st == "tow":
        provider = session.exec(
            select(TowTruckDriver).where(TowTruckDriver.user_id == caller.id)
        ).first()
    elif st == "mechanic":
        provider = session.exec(
            select(Mechanic).where(Mechanic.user_id == caller.id)
        ).first()
    else:
        raise HTTPException(400, f"Unsupported service_type '{st}'")

    if not provider or payment.payee_driver_id != provider.id:
        raise HTTPException(403, "Only the assigned provider can confirm this payment")


def _sync_booking(session: Session, payment: Payment) -> None:
    """Reflect a successful payment on the linked booking row."""
    cfg = _cfg(payment.service_type)
    booking = session.get(cfg["model"], payment.service_id)
    if not booking:
        return
    booking.payment_status = "paid"
    session.add(booking)
    audit_emit(
        "booking.payment_synced",
        trip_id=None,
        actor="system",
        payload={
            "service_type": payment.service_type,
            "service_reference_id": payment.service_reference_id,
            "payment_reference": payment.reference_id,
        },
    )


def refund_payment(
    session: Session,
    payment: Payment,
    reason: Optional[str] = None,
    *,
    actor: str = "admin",
    actor_id: Optional[str] = None,
) -> Payment:
    """Refund a succeeded payment.

    Routing (per product decision): a payment made FROM the wallet is refunded
    back INTO the wallet; gateway (platform) refunds go to the original source;
    cash/upi_direct are settled offline by the provider. Idempotent on an
    already-refunded payment.
    """
    if payment.status == "refunded":
        return payment
    if payment.status != "succeeded":
        raise HTTPException(400, "Only succeeded payments can be refunded")

    if payment.channel == "wallet":
        payer = session.get(User, payment.user_id)
        if not payer:
            raise HTTPException(404, "Payer not found for wallet refund")
        from app.modules.wallet import service as wallet_service

        wallet_service.credit(
            session,
            payer,
            payment.amount,
            source="refund",
            note=reason or "Booking refund",
            payment_reference=payment.reference_id,
            related_service_type=payment.service_type,
            related_service_reference_id=payment.service_reference_id,
            enforce_cap=False,
            allow_inactive=True,
        )
    elif payment.channel == "platform":
        payment.gateway_transaction_id = (
            payment.gateway_transaction_id or f"REFUND_{payment.reference_id}"
        )
    # cash / upi_direct: money never flowed through us; provider returns it.

    payment.status = "refunded"
    payment.updated_at = now_ist()
    payment.extra = {**(payment.extra or {}), "refund_reason": reason}
    session.add(payment)

    # Reflect the reversal on the linked booking.
    cfg = _cfg(payment.service_type)
    booking = session.get(cfg["model"], payment.service_id)
    if booking:
        booking.payment_status = "refunded"
        session.add(booking)

    session.commit()
    session.refresh(payment)

    audit_emit(
        "payment.refunded",
        trip_id=None,
        actor=actor,
        actor_id=actor_id,
        payload={
            "payment_reference": payment.reference_id,
            "channel": payment.channel,
            "amount": payment.amount,
            "reason": reason,
        },
    )
    return payment


def refund_booking_payments(
    session: Session,
    service_type: str,
    service_reference_id: str,
    reason: Optional[str] = None,
    *,
    actor: str = "user",
    actor_id: Optional[str] = None,
    channels: Optional[set] = None,
) -> list:
    """Best-effort refund of a cancelled booking's succeeded payments.

    Called from the tow / mechanic / service cancellation endpoints. By default
    (``channels={"wallet"}``) only wallet-paid charges are auto-refunded — they
    return to the wallet instantly — while gateway/cash refunds stay a manual
    admin action (unchanged behaviour). A refund failure NEVER blocks the
    cancellation: it is logged and skipped. Returns the refunded references.
    """
    if channels is None:
        channels = {"wallet"}

    payments = session.exec(
        select(Payment).where(
            Payment.service_type == service_type,
            Payment.service_reference_id == service_reference_id,
            Payment.status == "succeeded",
        )
    ).all()

    refunded = []
    for p in payments:
        if p.channel not in channels:
            continue
        try:
            refund_payment(session, p, reason, actor=actor, actor_id=actor_id)
            refunded.append(p.reference_id)
        except Exception:
            session.rollback()
            audit_emit(
                "payment.refund_failed",
                trip_id=None,
                actor=actor,
                actor_id=actor_id,
                payload={"payment_reference": p.reference_id, "reason": reason},
                severity="warning",
            )
    return refunded
