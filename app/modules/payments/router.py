"""
Centralized payment endpoints (tow / mechanic / service-center bookings).

  POST /payments/create-intent       user starts a payment (platform or direct)
  POST /payments/webhook             gateway -> us; verifies signature, settles
  POST /payments/{ref}/mark-paid     provider confirms cash/UPI collected
  GET  /payments/{ref}               payer/provider reads safe status
"""

from __future__ import annotations

from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Request,
)
from sqlmodel import Session

from app.core.database import get_session
from app.core.idempotency import IdempotencyGuard, idempotent
from app.core.models import Payment, PaymentIntentCreate, PaymentPublic, User
from app.core.security import get_current_user, get_current_admin
from app.modules.payments import service as payment_service
from app.utils.id_generator import get_by_reference

router = APIRouter(prefix="/payments", tags=["Payments"])


@router.post("/create-intent")
def create_intent(
    data: PaymentIntentCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", convert_underscores=False
    ),
    guard: IdempotencyGuard = Depends(idempotent("payment.create")),
):
    if guard.cached_response is not None:
        return guard.cached_response

    payment, client_secret = payment_service.create_payment_intent(
        session, current_user, data, idempotency_key=idempotency_key
    )

    # Mock async settlement for gateway payments (card/UPI): deliver a signed
    # webhook shortly after. A real gateway would call /payments/webhook OOB.
    if payment.channel in ("platform", "upi") and payment.status == "pending":
        background_tasks.add_task(
            payment_service.simulate_webhook_delivery, payment.reference_id
        )

    result = {
        "payment": PaymentPublic.model_validate(
            payment, from_attributes=True
        ).model_dump(mode="json"),
        "client_secret": client_secret,
    }
    guard.store(result)
    return result


@router.post("/webhook")
async def gateway_webhook(
    request: Request,
    x_payment_signature: Optional[str] = Header(
        default=None, alias="X-Payment-Signature"
    ),
    session: Session = Depends(get_session),
):
    raw = (await request.body()).decode("utf-8")
    return payment_service.handle_webhook(session, raw, x_payment_signature)


@router.post("/{payment_ref}/mark-paid", response_model=PaymentPublic)
def mark_paid(
    payment_ref: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    payment = get_by_reference(session, Payment, payment_ref)
    if not payment:
        raise HTTPException(404, "Payment not found")
    return payment_service.mark_direct_paid(session, payment, current_user)


@router.post("/{payment_ref}/refund", response_model=PaymentPublic)
def refund_payment(
    payment_ref: str,
    reason: str = Body(default=None, embed=True),
    session: Session = Depends(get_session),
    admin: User = Depends(get_current_admin),
):
    """Admin-initiated refund. Wallet-paid bookings refund to the wallet; gateway
    payments refund to source; cash/UPI-direct are returned offline by the provider."""
    payment = get_by_reference(session, Payment, payment_ref)
    if not payment:
        raise HTTPException(404, "Payment not found")
    return payment_service.refund_payment(
        session, payment, reason, actor="admin", actor_id=str(admin.id)
    )


@router.get("/{payment_ref}", response_model=PaymentPublic)
def get_payment(
    payment_ref: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    payment = get_by_reference(session, Payment, payment_ref)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.user_id != current_user.id:
        # Allow the assigned provider to read it too; otherwise 403.
        payment_service._authorize_provider(session, payment, current_user)
    return payment
