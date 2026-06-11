"""
Trip-specific side effects for centralized payments.

Trips make many charges per booking (driver acceptance fee, user upfront, daily
bills, settlement, cancellation balance). The money movement lives on the
centralized ``Payment`` ledger (app.modules.payments.service), but the trip
lifecycle reactions to a *successful* charge — settling a TripBill, unblocking
OTP, generating the shift schedule, closing a settlement — live here.

``on_trip_payment_succeeded`` is the single hook the central service calls the
first time a trip Payment reaches ``succeeded``. It fires from all three
settlement timings identically:
  * wallet  — synchronously inside create_trip_payment_intent,
  * cash    — synchronously when the driver confirms collection,
  * platform— asynchronously on the gateway webhook.

It runs INSIDE the caller's open transaction and never commits, so the charge
and its side effect persist atomically. ``refund_trip_amount`` is the inverse:
it allocates a computed refund across the trip's succeeded user charges via the
central partial-refund path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.models import (
    Payment,
    Trip,
    TripAttendance,
    TripBill,
    TripSettlement,
)
from app.services.audit_log import emit_event as audit_emit
from app.utils.time_utils import now_ist

_EPS = 1e-6


def on_trip_payment_succeeded(session: Session, payment: Payment) -> None:
    if payment.service_type != "trip":
        return
    trip = session.get(Trip, payment.service_id)
    if not trip:
        return

    purpose = payment.purpose
    if purpose == "driver_acceptance":
        _handle_driver_acceptance(session, payment, trip)
    elif purpose == "user_upfront":
        _handle_user_upfront(session, payment, trip)
    elif purpose in ("daily_bill", "cancellation_balance", "schedule_diff"):
        _handle_bill_payment(session, payment, trip)
    elif purpose == "settlement":
        _handle_settlement_payment(session, payment, trip)

    _emit_legacy_event(payment, trip)


def _handle_driver_acceptance(session: Session, payment: Payment, trip: Trip) -> None:
    trip.driver_payment_status = "paid"
    trip.driver_payment_amount = payment.amount
    session.add(trip)
    finalize_driver_acceptance(session, trip)


def finalize_driver_acceptance(session: Session, trip: Trip) -> None:
    from app.modules.trips.trip_service import TripService

    trip_service = TripService()

    if trip.start_date:
        duration_hours = (
            trip_service.get_trip_duration_hours(trip.shift_details)
            or trip.trip_duration_hours
            or 8
        )
        parsed_time = trip_service.get_trip_start_time(
            trip.shift_details, trip.start_date
        )
        effective_start_dt = (
            parsed_time
            or trip.scheduled_start_time
            or datetime.combine(trip.start_date, datetime.min.time())
        )
        if parsed_time and not trip.scheduled_start_time:
            trip.scheduled_start_time = parsed_time
            trip.scheduled_end_time = parsed_time + timedelta(hours=duration_hours)
            trip.trip_duration_hours = duration_hours

        is_outstation = (trip.hiring_type or "").strip().lower() == "outstation"
        end_date_for_attendance = trip.end_date or trip.start_date
        att_ok, att_err = trip_service.create_trip_attendance_records(
            session,
            trip.id,
            trip.start_date,
            end_date_for_attendance,
            effective_start_dt,
            duration_hours,
            selected_days=trip.selected_days,
            single_shift=is_outstation,
            commit=False,
        )
        if not att_ok:
            raise HTTPException(400, att_err)

        if is_outstation:
            single_att = session.exec(
                select(TripAttendance).where(TripAttendance.trip_id == trip.id)
            ).first()
            if single_att:
                trip.scheduled_start_time = single_att.scheduled_start
                trip.scheduled_end_time = single_att.scheduled_end
                session.add(trip)

    ok, err = trip_service.transition_trip_state(
        session, trip.id, "active_pending_otp", validate=True, commit=False
    )
    if not ok:
        raise HTTPException(400, f"Status update failed: {err}")

    if trip.payment_method in ("advance_20", "full_payment"):
        trip.status = "paused"
        trip.is_payment_blocked = True
        trip.state_version += 1
        session.add(trip)


def _handle_user_upfront(session: Session, payment: Payment, trip: Trip) -> None:
    apply_user_upfront_settlement(session, trip)


def apply_user_upfront_settlement(session: Session, trip: Trip) -> None:
    from app.modules.trips.trip_service import TripService
    from app.modules.trips.billing_service import payment_method_discount_pct

    trip_service = TripService()
    portion = trip_service.compute_outstanding_portion(session, trip)
    discount_pct = payment_method_discount_pct(trip.hiring_type, trip.payment_method)
    per_day = portion["per_day"]

    total_upfront = _sum_user_payments(
        session, trip.id, exclude_purposes=("daily_bill", "settlement")
    )
    trip_day_cash = _sum_user_payments(session, trip.id, only_purposes=("daily_bill",))

    daily_bills = session.exec(
        select(TripBill)
        .where(
            TripBill.trip_id == trip.id,
            TripBill.bill_type == "daily_bill",
        )
        .order_by(TripBill.bill_date)
        .with_for_update()
    ).all()

    running_paid = 0.0
    now = now_ist()
    for bill in daily_bills:
        if (bill.amount_due or 0.0) > 0:
            disc = round(per_day * discount_pct / 100.0, 2)
            net = round(per_day - disc, 2)
            credit_available = max(
                0.0, total_upfront - max(0.0, running_paid - trip_day_cash)
            )
            paid = min(net, credit_available)
            bill.discount_percentage = discount_pct or None
            bill.discount_amount = disc
            bill.total_amount = net
            bill.amount_paid = paid
            bill.amount_due = max(0.0, round(net - paid, 2))
            bill.is_paid = bill.amount_due <= 0
            if bill.is_paid:
                bill.paid_at = now
                bill.paid_by = "user_online"
            if disc > 0 and isinstance(bill.components, list):
                bill.components = bill.components + [
                    {
                        "name": f"Payment Discount ({discount_pct:.0f}%)",
                        "amount": -disc,
                        "percentage": -discount_pct,
                    }
                ]
            session.add(bill)
        running_paid += bill.amount_paid or 0.0

    trip.is_payment_blocked = False
    for att in session.exec(
        select(TripAttendance).where(
            TripAttendance.trip_id == trip.id,
            TripAttendance.status == "paused_payment",
        )
    ).all():
        att.status = "scheduled"
        att.skip_reason = None
        att.marked_by = "system"
        session.add(att)
    if trip.status == "paused":
        trip.status = (
            "active_pending_otp"
            if trip_service.has_pending_shifts(session, trip.id)
            else "completed"
        )
    trip.state_version += 1
    session.add(trip)


def _handle_bill_payment(session: Session, payment: Payment, trip: Trip) -> None:
    from app.modules.trips.payment_service import PaymentService

    bill_id = (payment.extra or {}).get("bill_id")
    bill = session.get(TripBill, bill_id) if bill_id else None
    if bill and not bill.is_paid:
        bill.amount_paid = bill.total_amount
        bill.amount_due = 0.0
        bill.is_paid = True
        bill.paid_at = now_ist()
        if payment.channel == "cash":
            bill.paid_by = "driver_offline"
            bill.paid_by_driver_id = payment.payee_driver_id
        else:
            bill.paid_by = "user_online"
        note = (payment.extra or {}).get("note")
        if note:
            bill.payment_note = note
        session.add(bill)

    PaymentService(None).unpause_trip_if_clear(session, trip.id)

    if payment.purpose == "cancellation_balance" and trip.status == (
        "cancellation_pending_payment"
    ):
        from app.modules.trips.trip_service import TripService

        TripService().transition_trip_state(
            session, trip.id, "settled", validate=False, commit=False
        )


def _handle_settlement_payment(session: Session, payment: Payment, trip: Trip) -> None:
    from app.modules.trips.payment_service import PaymentService

    settlement_id = (payment.extra or {}).get("settlement_id")
    settlement = session.get(TripSettlement, settlement_id) if settlement_id else None
    if not settlement or settlement.user_payment_status == "paid":
        return

    settlement.user_payment_status = "paid"
    settlement.paid_at = now_ist()
    note = (payment.extra or {}).get("note")
    if note:
        settlement.payment_note = note

    extra_amount = float((payment.extra or {}).get("extra_amount") or 0.0)
    if extra_amount > 0:
        settlement.extra_amount_paid = round(
            (settlement.extra_amount_paid or 0.0) + extra_amount, 2
        )
        merged = dict(settlement.extra_amount_breakdown or {})
        for k, v in ((payment.extra or {}).get("extra_amount_breakdown") or {}).items():
            merged[k] = round(float(merged.get(k, 0.0)) + float(v), 2)
        settlement.extra_amount_breakdown = merged
    session.add(settlement)

    unpaid_bills = session.exec(
        select(TripBill).where(
            TripBill.trip_id == settlement.trip_id,
            TripBill.is_paid == False,
        )
    ).all()
    paid_at_now = now_ist()
    for bill in unpaid_bills:
        bill.is_paid = True
        bill.amount_paid = bill.total_amount
        bill.amount_due = 0.0
        bill.paid_at = paid_at_now
        bill.paid_by = "user_online"
        session.add(bill)

    PaymentService(None).unpause_trip_if_clear(session, settlement.trip_id)

    if trip.status == "billed":
        trip.status = "settled"
        trip.state_version += 1
        session.add(trip)


def refund_trip_amount(
    session: Session,
    trip_id: int,
    amount: float,
    reason: str,
    *,
    actor: str = "system",
    actor_id: Optional[str] = None,
) -> float:
    from app.modules.payments import service as payment_service

    remaining = round(float(amount), 2)
    if remaining <= 0:
        return 0.0

    payments = session.exec(
        select(Payment)
        .where(
            Payment.service_type == "trip",
            Payment.service_id == trip_id,
            Payment.payer_type == "user",
            Payment.channel != "credit",
            Payment.status.in_(["succeeded", "partially_refunded"]),
        )
        .order_by(Payment.id)
    ).all()

    refunded_total = 0.0
    for p in payments:
        if remaining <= _EPS:
            break
        avail = round(p.amount - (p.refunded_amount or 0.0), 2)
        if avail <= 0:
            continue
        portion = round(min(avail, remaining), 2)
        payment_service.refund_payment(
            session, p, reason, amount=portion, actor=actor, actor_id=actor_id
        )
        refunded_total = round(refunded_total + portion, 2)
        remaining = round(remaining - portion, 2)

    if refunded_total > 0:
        audit_emit(
            "refund.user",
            trip_id=trip_id,
            actor=actor,
            actor_id=actor_id,
            payload={"amount": refunded_total, "reason": reason},
        )
    return refunded_total


def _sum_user_payments(
    session: Session,
    trip_id: int,
    *,
    only_purposes: Optional[tuple] = None,
    exclude_purposes: Optional[tuple] = None,
) -> float:
    stmt = select(Payment).where(
        Payment.service_type == "trip",
        Payment.service_id == trip_id,
        Payment.payer_type == "user",
        Payment.status.in_(["succeeded", "partially_refunded"]),
    )
    if only_purposes is not None:
        stmt = stmt.where(Payment.purpose.in_(list(only_purposes)))
    if exclude_purposes is not None:
        stmt = stmt.where(Payment.purpose.notin_(list(exclude_purposes)))
    return round(sum(p.amount for p in session.exec(stmt).all()), 2)


_LEGACY_EVENT_BY_PURPOSE = {
    "driver_acceptance": "payment.driver_acceptance",
    "user_upfront": "payment.user_upfront",
    "daily_bill": "payment.daily_bill",
}


def _emit_legacy_event(payment: Payment, trip: Trip) -> None:
    event = _LEGACY_EVENT_BY_PURPOSE.get(payment.purpose or "")
    if not event:
        return
    audit_emit(
        event,
        trip_id=trip.id,
        actor="driver" if payment.payer_type == "driver" else "user",
        actor_id=str(payment.user_id),
        payload={
            "amount": payment.amount,
            "channel": payment.channel,
            "payment_reference": payment.reference_id,
            "purpose": payment.purpose,
        },
    )
