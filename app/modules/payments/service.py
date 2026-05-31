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
(_sync_booking). Trip flows route through create_trip_payment_intent: they
carry a ``purpose`` + ``payer_type`` and their post-payment side effects (bill
settlement, state transitions, schedule generation) are owned by the trip
orchestrator hook (app.modules.trips.payment_orchestrator) rather than the
generic _sync_booking, which no-ops for trips (service map ``sync=False``).
The billing-domain tables (TripBill / TripSettlement) are unchanged.
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
    Trip,
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
    TRIP,
)
from app.utils.time_utils import now_ist

DIRECT_CHANNELS = {"cash", "upi_direct"}

ALL_CHANNELS = {"platform", "wallet"} | DIRECT_CHANNELS

_EPS = 1e-6  # float tolerance for money comparisons

# service_type -> how to resolve the booking, its amount, and its provider.
# ``sync`` (default True) controls whether a successful payment flips the
# booking's ``payment_status`` to "paid"/"refunded" via _sync_booking. Trips
# have no single ``payment_status`` column (they track driver_payment_status /
# bills / settlement), so ``sync=False`` and the trip orchestrator owns the
# post-payment side effects instead.
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
    "trip": {
        "model": Trip,
        "entity": TRIP,
        "amount_attrs": (),  # no single fare — every trip charge passes an amount
        "provider_attr": "driver_id",
        "sync": False,  # orchestrator owns trip post-payment side effects
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


def _find_pending_trip_payment(
    session: Session,
    trip_id: int,
    purpose: str,
    extra: Optional[Dict[str, Any]],
) -> Optional[Payment]:
    """An already-open (pending) platform charge for the same trip line item.

    Used to replay rather than re-create a gateway intent when a delayed webhook
    makes the client retry — prevents a double charge. Bill/settlement charges
    are matched on their bill_id/settlement_id; per-trip charges (driver
    acceptance, upfront) on purpose alone."""
    rows = session.exec(
        select(Payment).where(
            Payment.service_type == "trip",
            Payment.service_id == trip_id,
            Payment.purpose == purpose,
            Payment.status == "pending",
        )
    ).all()
    if not rows:
        return None
    extra = extra or {}
    line_key = None
    if purpose in ("daily_bill", "cancellation_balance", "schedule_diff"):
        line_key = ("bill_id", extra.get("bill_id"))
    elif purpose == "settlement":
        line_key = ("settlement_id", extra.get("settlement_id"))
    if line_key and line_key[1] is not None:
        for r in rows:
            if (r.extra or {}).get(line_key[0]) == line_key[1]:
                return r
        return None
    return rows[0]


def create_trip_payment_intent(
    session: Session,
    *,
    trip: Trip,
    purpose: str,
    amount: float,
    payer_type: str,
    payer_user: User,
    payer_driver_id: Optional[int] = None,
    channel: str,
    card_reference_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[Payment, Optional[str]]:
    """Create (or replay) one trip charge on the centralized Payment ledger.

    Each trip charge is a single Payment row carrying a ``purpose`` and a
    ``payer_type``. Channel routing mirrors the public create_payment_intent:
      * ``wallet``   — debit now, succeed synchronously, fire the trip hook.
      * ``cash``     — succeed now (collected offline by the driver), fire hook.
      * ``platform`` — gateway intent; settles async on the webhook (hook then).
    The caller is responsible for authorizing the payer. Returns
    ``(payment, client_secret)``; client_secret is set only for platform."""
    if channel not in ALL_CHANNELS:
        raise HTTPException(400, f"channel must be one of {sorted(ALL_CHANNELS)}")
    if payer_type not in ("user", "driver"):
        raise HTTPException(400, "payer_type must be 'user' or 'driver'")
    if not trip or not trip.reference_id:
        raise HTTPException(404, "Trip not found for payment")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(400, "Could not determine a positive payment amount")

    # DB-level idempotency: same key replays the existing payment untouched.
    if idempotency_key:
        existing = session.exec(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        ).first()
        if existing:
            return existing, (existing.extra or {}).get("client_secret")

    # One open platform intent per trip line item — guards against a double
    # charge when the user retries while a previous webhook is still in flight.
    if channel == "platform":
        pending = _find_pending_trip_payment(session, trip.id, purpose, extra)
        if pending:
            return pending, (pending.extra or {}).get("client_secret")

    # Driver-collected daily-bill cash is reconciled to the driver via
    # payee_driver_id (reporting only); every other charge is platform revenue.
    payee_type = "platform"
    payee_driver_id = None
    if channel == "cash" and purpose in ("daily_bill", "cancellation_balance"):
        payee_type = "provider"
        payee_driver_id = trip.driver_id

    payment = Payment(
        reference_id=generate_reference_id(session, PAYMENT),
        service_type="trip",
        service_reference_id=trip.reference_id,
        service_id=trip.id,
        user_id=payer_user.id,
        payer_type=payer_type,
        payer_driver_id=payer_driver_id,
        payee_type=payee_type,
        payee_driver_id=payee_driver_id,
        purpose=purpose,
        amount=amount,
        channel=channel,
        idempotency_key=idempotency_key,
        extra=dict(extra or {}),
    )

    client_secret: Optional[str] = None
    if channel == "platform":
        if card_reference_id:
            payment.extra["card_reference"] = _validate_card_reference(
                session, payer_user, card_reference_id
            )
        intent = gateway.create_intent(
            amount,
            metadata={
                "payment_reference": payment.reference_id,
                "service_type": "trip",
                "service_reference_id": trip.reference_id,
                "purpose": purpose,
            },
        )
        payment.gateway_provider = gateway.PROVIDER
        payment.gateway_intent_id = intent["intent_id"]
        payment.status = "pending"
        client_secret = intent["client_secret"]
        payment.extra["client_secret"] = client_secret
    elif channel == "wallet":
        wallet_txn = wallet_service.debit_for_payment(
            session, payer_user, amount, payment
        )
        payment.status = "succeeded"
        payment.completed_at = now_ist()
        payment.extra["wallet_txn_reference"] = wallet_txn.reference_id
        session.add(payment)
        _post_success(session, payment)
    elif channel == "cash":
        # Collected offline by the driver; the endpoint already authorized them.
        payment.status = "succeeded"
        payment.gateway_transaction_id = f"DIRECT_cash_{payment.reference_id}"
        payment.completed_at = now_ist()
        session.add(payment)
        _post_success(session, payment)
    else:  # upi_direct — awaits provider mark-paid (not used by trips today)
        payment.status = "created"

    session.add(payment)
    session.commit()
    session.refresh(payment)

    audit_emit(
        "payment.intent_created",
        trip_id=trip.id,
        actor="driver" if payer_type == "driver" else "user",
        actor_id=str(payer_user.id),
        payload={
            "payment_reference": payment.reference_id,
            "service_type": "trip",
            "service_reference_id": trip.reference_id,
            "purpose": purpose,
            "channel": channel,
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
    _post_success(session, payment)
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
    _post_success(session, payment)
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
    """Reflect a successful payment on the linked booking row.

    No-op for services with ``sync=False`` (trips): the Trip row has no single
    ``payment_status`` column and its post-payment side effects are owned by the
    trip orchestrator hook instead."""
    cfg = _cfg(payment.service_type)
    if not cfg.get("sync", True):
        return
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


def _post_success(session: Session, payment: Payment) -> None:
    """Run side effects when a payment first reaches ``succeeded``.

    Generic booking sync for tow/mechanic/service, plus the trip orchestrator
    hook for trip charges. Runs inside the caller's open transaction. The trip
    import is local to avoid a trips<->payments circular import."""
    _sync_booking(session, payment)
    if payment.service_type == "trip":
        from app.modules.trips.payment_orchestrator import on_trip_payment_succeeded

        on_trip_payment_succeeded(session, payment)


def refund_payment(
    session: Session,
    payment: Payment,
    reason: Optional[str] = None,
    *,
    amount: Optional[float] = None,
    actor: str = "admin",
    actor_id: Optional[str] = None,
) -> Payment:
    """Refund a succeeded payment, fully or partially.

    ``amount=None`` refunds the full not-yet-refunded remainder (the historical
    behaviour every tow/mechanic/service caller relies on). Passing ``amount``
    refunds just that much and leaves the payment ``partially_refunded`` until
    fully drained — used by trip cancellation maths where the refund is a
    computed slice of an upfront charge.

    Routing (per product decision): a payment made FROM the wallet is refunded
    back INTO the wallet; gateway (platform) refunds go to the original source;
    cash/upi_direct are settled offline by the provider. Idempotent once the
    payment is fully refunded.
    """
    if payment.status == "refunded":
        return payment
    if payment.status not in ("succeeded", "partially_refunded"):
        raise HTTPException(400, "Only succeeded payments can be refunded")

    remaining = round(payment.amount - (payment.refunded_amount or 0.0), 2)
    this_refund = remaining if amount is None else round(float(amount), 2)
    if this_refund <= 0:
        raise HTTPException(400, "Refund amount must be positive")
    if this_refund > remaining + _EPS:
        raise HTTPException(
            400,
            f"Refund of ₹{this_refund:.2f} exceeds the refundable remainder "
            f"(₹{remaining:.2f}) on payment {payment.reference_id}",
        )

    if payment.channel == "wallet":
        payer = session.get(User, payment.user_id)
        if not payer:
            raise HTTPException(404, "Payer not found for wallet refund")
        from app.modules.wallet import service as wallet_service

        wallet_service.credit(
            session,
            payer,
            this_refund,
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

    payment.refunded_amount = round((payment.refunded_amount or 0.0) + this_refund, 2)
    fully_refunded = payment.refunded_amount >= payment.amount - _EPS
    payment.status = "refunded" if fully_refunded else "partially_refunded"
    payment.updated_at = now_ist()
    payment.extra = {**(payment.extra or {}), "refund_reason": reason}
    session.add(payment)

    # Reflect the reversal on the linked booking (skipped for trips, which have
    # no payment_status column — sync=False). Only flip to "refunded" on a full
    # refund so a partial does not mislabel the booking.
    cfg = _cfg(payment.service_type)
    if cfg.get("sync", True) and fully_refunded:
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
            "amount": this_refund,
            "refunded_amount": payment.refunded_amount,
            "fully_refunded": fully_refunded,
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
