"""
Trip payment service — a thin, trip-aware wrapper over the centralized Payment
ledger (app.modules.payments.service).

Each trip charge becomes one ``Payment`` row carrying a ``purpose`` and a
``payer_type``; the gateway call, wallet debit, and refunds are all owned by the
central service. This module keeps the trip-specific concerns: resolving the
driver's User for the driver-paid acceptance fee, the cancel/abandon refund
maths, the outstanding-dues gates, and the OTP-pause logic. The post-payment
side effects (settling a TripBill, unblocking OTP, generating the schedule,
closing a settlement) live in app.modules.trips.payment_orchestrator and fire
identically on the sync (wallet/cash) and async (platform webhook) paths.
"""

from typing import Optional, Tuple
from sqlmodel import Session, select
from fastapi import HTTPException
import redis

from app.core.models import (
    Trip,
    Payment,
    TripAttendance,
    TripBill,
    TripSettlement,
    SystemConfig,
    Driver,
    User,
)
from app.services.audit_log import emit_event as audit_emit

# A trip charge counts as paid-in while succeeded or partially refunded.
_PAID_IN_STATES = ["succeeded", "partially_refunded"]


def _resolve_driver_user(session: Session, driver_id: int) -> Optional[User]:
    """The User account behind a Driver PK (driver-paid charges debit/refund it)."""
    return session.exec(
        select(User).join(Driver, Driver.user_id == User.id).where(Driver.id == driver_id)
    ).first()


def _bill_purpose(bill: TripBill) -> str:
    """Map a bill's type to the Payment.purpose the orchestrator dispatches on."""
    if bill.bill_type in ("daily_bill", "cancellation_balance", "schedule_diff"):
        return bill.bill_type
    return "daily_bill"

# Key in SystemConfig table (admin-editable via /admin/system-config).
DRIVER_ACCEPTANCE_FEE_KEY = "driver_acceptance_fee"
DEFAULT_DRIVER_ACCEPTANCE_FEE = 100.0


def get_driver_acceptance_fee(
    session: Session,
    redis_client: Optional[redis.Redis] = None,
) -> float:
    """Resolve the driver acceptance fee. Redis → SystemConfig → hard-coded default."""
    if redis_client is not None:
        try:
            cached = redis_client.get(f"config:{DRIVER_ACCEPTANCE_FEE_KEY}")
            if cached:
                if isinstance(cached, bytes):
                    cached = cached.decode()
                return float(cached)
        except (redis.RedisError, ValueError, TypeError):
            pass

    cfg = session.get(SystemConfig, DRIVER_ACCEPTANCE_FEE_KEY)
    if cfg and cfg.value:
        try:
            value = float(cfg.value)
            if redis_client is not None:
                try:
                    redis_client.set(f"config:{DRIVER_ACCEPTANCE_FEE_KEY}", cfg.value)
                except redis.RedisError:
                    pass
            return value
        except (TypeError, ValueError):
            pass

    return DEFAULT_DRIVER_ACCEPTANCE_FEE


class PaymentService:
    """
    Manages payment processing for both driver and user
    - Dummy payment gateway integration
    - Refund logic based on payment method
    - State machine validation
    """

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self.redis = redis_client

    def driver_accept_payment(
        self,
        session: Session,
        trip_id: int,
        driver_id: int,
        *,
        channel: str = "platform",
        card_reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Optional[Payment], Optional[str], Optional[str]]:
        """Charge the driver acceptance fee on the central ledger.

        Returns ``(payment, client_secret, error)``. For wallet/cash the fee
        settles synchronously and the orchestrator flips driver_payment_status
        to "paid" + arms the trip; for platform it stays pending until the
        gateway webhook fires that same hook. ``client_secret`` is set only for
        platform charges."""
        from app.modules.payments import service as central
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return None, None, "Trip not found"
            if trip.driver_id != driver_id:
                return None, None, "Driver not matched to this trip"
            if trip.driver_payment_status != "unpaid":
                return None, None, f"Driver payment already {trip.driver_payment_status}"

            driver_user = _resolve_driver_user(session, driver_id)
            if not driver_user:
                return None, None, "Driver account not found"

            fee = get_driver_acceptance_fee(session, self.redis)
            payment, client_secret = central.create_trip_payment_intent(
                session,
                trip=trip,
                purpose="driver_acceptance",
                amount=fee,
                payer_type="driver",
                payer_user=driver_user,
                payer_driver_id=driver_id,
                channel=channel,
                card_reference_id=card_reference_id,
                idempotency_key=idempotency_key,
            )
            return payment, client_secret, None
        except HTTPException as e:
            return None, None, e.detail
        except Exception as e:
            return None, None, f"Driver payment processing failed: {str(e)}"

    def user_make_payment(
        self,
        session: Session,
        trip: Trip,
        user: User,
        amount: float,
        *,
        channel: str = "platform",
        card_reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Optional[Payment], Optional[str], Optional[str]]:
        """User upfront (advance_20 / full_payment) charge on the central ledger.

        Returns ``(payment, client_secret, error)``. For wallet the upfront
        settles synchronously and the orchestrator un-blocks the trip; for
        platform it stays pending until the webhook fires that hook."""
        from app.modules.payments import service as central
        try:
            if amount is None or amount <= 0:
                return None, None, "Invalid amount"
            if trip.user_id != user.id:
                return None, None, "User not matched to this trip"
            payment, client_secret = central.create_trip_payment_intent(
                session,
                trip=trip,
                purpose="user_upfront",
                amount=amount,
                payer_type="user",
                payer_user=user,
                channel=channel,
                card_reference_id=card_reference_id,
                idempotency_key=idempotency_key,
            )
            return payment, client_secret, None
        except HTTPException as e:
            return None, None, e.detail
        except Exception as e:
            return None, None, f"User payment processing failed: {str(e)}"

    def _user_paid_total(self, session: Session, trip_id: int) -> float:
        """Gross successful USER charges on a trip (matches the legacy sum of
        success rows; a partial refund leaves the charge amount intact)."""
        return sum(
            p.amount
            for p in session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_type == "user",
                    Payment.status.in_(_PAID_IN_STATES),
                )
            ).all()
        )

    def calculate_driver_fee_refund(
        self, session: Session, trip_id: int, driver_id: Optional[int]
    ) -> float:
        """Acceptance fee paid (and not yet refunded) for this driver on this trip."""
        if not driver_id:
            return 0.0
        paid = session.exec(
            select(Payment).where(
                Payment.service_type == "trip",
                Payment.service_id == trip_id,
                Payment.payer_driver_id == driver_id,
                Payment.payer_type == "driver",
                Payment.purpose == "driver_acceptance",
                Payment.status.in_(_PAID_IN_STATES),
            )
        ).first()
        # A fully refunded fee has status "refunded" (excluded above) → 0; a
        # partial leaves the refundable remainder.
        if not paid:
            return 0.0
        return round(max(0.0, paid.amount - (paid.refunded_amount or 0.0)), 2)

    def calculate_user_cancel_settlement(self, session: Session, trip_id: int) -> dict:
        """Final refund/shortfall maths for an advance_20 / full_payment cancel (F6).

        Formula::

            net = upfront_paid − (days_served × per_day_rate) − (₹50 × user_skipped_days)

        Returns a dict::

            {
                "upfront_paid":   float,   # total successful user payments
                "served_days":    int,
                "skipped_user_days": int,
                "per_day_rate":   float,
                "served_charge":  float,   # served_days × per_day_rate
                "anti_fraud":     float,   # 50 * skipped_user_days
                "refund_amount":  float,   # max(net, 0)
                "shortfall_amount": float, # max(-net, 0)
            }

        Same shape regardless of refund vs. shortfall so the caller can switch
        on whichever is non-zero. ``per_day_rate = trip.fare / total_days``;
        ``total_days`` is the count of TripAttendance rows on the booking
        (falls back to 1 if there are none — should not happen post-payment).
        """
        trip = session.get(Trip, trip_id)
        if not trip:
            return {
                "upfront_paid": 0.0,
                "served_days": 0,
                "skipped_user_days": 0,
                "per_day_rate": 0.0,
                "served_charge": 0.0,
                "anti_fraud": 0.0,
                "refund_amount": 0.0,
                "shortfall_amount": 0.0,
            }

        attendances = session.exec(
            select(TripAttendance).where(TripAttendance.trip_id == trip_id)
        ).all()
        total_days = len(attendances) or 1
        served_days = sum(1 for a in attendances if a.status == "present")
        # Anti-fraud counts BOTH user-initiated and system-marked no-shows
        # (Issue 5). Driver skips are excluded — not the user's fault.
        skipped_user_days = sum(
            1
            for a in attendances
            if a.status in ("skipped_by_user", "skipped_by_system")
        )

        upfront_paid = self._user_paid_total(session, trip_id)

        per_day_rate = float(trip.fare or 0.0) / total_days
        served_charge = round(per_day_rate * served_days, 2)
        anti_fraud = round(50.0 * skipped_user_days, 2)
        net = round(upfront_paid - served_charge - anti_fraud, 2)

        return {
            "upfront_paid": round(upfront_paid, 2),
            "served_days": served_days,
            "skipped_user_days": skipped_user_days,
            "per_day_rate": round(per_day_rate, 2),
            "served_charge": served_charge,
            "anti_fraud": anti_fraud,
            "refund_amount": round(max(net, 0.0), 2),
            "shortfall_amount": round(max(-net, 0.0), 2),
        }

    def calculate_driver_abandon_refund(self, session: Session, trip_id: int) -> float:
        """User refund when the driver abandons a trip mid-booking (F11).

        Pro-rated: ``unused_days * per_day_rate``, capped at the amount the
        user actually paid so we never refund more than they put in. The user
        is blameless here so no anti-fraud deduction is applied.
        """
        trip = session.get(Trip, trip_id)
        if not trip:
            return 0.0

        attendances = session.exec(
            select(TripAttendance).where(TripAttendance.trip_id == trip_id)
        ).all()
        total_days = len(attendances) or 1
        served_days = sum(1 for a in attendances if a.status == "present")
        unused_days = max(0, total_days - served_days)
        if unused_days == 0:
            return 0.0

        per_day = float(trip.fare or 0.0) / total_days
        gross_refund = per_day * unused_days

        total_paid = self._user_paid_total(session, trip_id)
        return round(min(gross_refund, total_paid), 2)

    def calculate_refund_amount(
        self, session: Session, trip_id: int
    ) -> Tuple[float, Optional[str]]:
        """Pre-F6 simple refund: total_paid if no shift started, else 0 (or
        prorated for full_payment). Mid-trip cancellations now use
        :meth:`calculate_user_cancel_settlement` which handles shortfalls too.
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return 0.0, "Trip not found"

            total_paid = self._user_paid_total(session, trip_id)

            if trip.payment_method == "trip_day":
                return (total_paid if trip.actual_start_time is None else 0.0), None

            if trip.payment_method == "advance_20":
                any_present = session.exec(
                    select(TripAttendance.id).where(
                        TripAttendance.trip_id == trip_id,
                        TripAttendance.status == "present",
                    )
                ).first()
                return (total_paid if any_present is None else 0.0), None

            if trip.payment_method == "full_payment":
                if not (trip.fare and trip.start_date and trip.end_date):
                    return 0.0, None
                daily_rate = trip.fare / ((trip.end_date - trip.start_date).days + 1)
                days_used = len(
                    session.exec(
                        select(TripAttendance.id).where(
                            TripAttendance.trip_id == trip_id,
                            TripAttendance.status == "present",
                        )
                    ).all()
                )
                return max(0.0, total_paid - daily_rate * days_used), None

            return total_paid, None

        except Exception as e:
            return 0.0, f"Refund calculation failed: {str(e)}"

    def refund_driver_acceptance_fee(
        self,
        session: Session,
        trip_id: int,
        driver_id: int,
        reason: str = "Driver rejected post-payment",
    ) -> Tuple[bool, Optional[str]]:
        """Refund the acceptance fee via the central ledger (wallet→wallet,
        platform→source). No-ops if never paid or already fully refunded."""
        from app.modules.payments import service as central

        try:
            paid = session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_driver_id == driver_id,
                    Payment.payer_type == "driver",
                    Payment.purpose == "driver_acceptance",
                    Payment.status.in_(_PAID_IN_STATES),
                )
            ).first()
            # None ⇒ never paid, or already fully refunded (status "refunded").
            if not paid:
                return True, None

            central.refund_payment(
                session, paid, reason, actor="system", actor_id=str(driver_id)
            )
            audit_emit(
                "refund.driver_acceptance",
                trip_id=trip_id,
                actor="system",
                actor_id=str(driver_id),
                payload={"amount": paid.amount, "reason": reason},
            )
            return True, None
        except HTTPException as e:
            session.rollback()
            return False, e.detail
        except Exception as e:
            session.rollback()
            return False, f"Driver fee refund failed: {e}"

    def pay_bill_online(
        self,
        session: Session,
        bill_id: int,
        user: User,
        *,
        channel: str = "platform",
        card_reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Tuple[Optional[Payment], Optional[str], Optional[str]]:
        """User pays a daily / cancellation_balance / schedule_diff bill online.

        Row-locks the bill, then charges it on the central ledger. For wallet
        the bill settles synchronously (the orchestrator marks it paid and
        unpauses the trip); for platform it stays open until the webhook fires
        that same hook. Returns ``(payment, client_secret, error)``."""
        from app.modules.payments import service as central
        try:
            bill = session.exec(
                select(TripBill).where(TripBill.id == bill_id).with_for_update()
            ).first()
            if not bill:
                return None, None, "Bill not found"
            if str(bill.user_id) != str(user.id):
                return None, None, "Not authorized for this bill"
            if bill.is_paid:
                return None, None, "Bill already paid"
            trip = session.get(Trip, bill.trip_id)
            if not trip:
                return None, None, "Trip not found"

            extra = {"bill_id": bill.id}
            if note:
                extra["note"] = note
            payment, client_secret = central.create_trip_payment_intent(
                session,
                trip=trip,
                purpose=_bill_purpose(bill),
                amount=bill.total_amount,
                payer_type="user",
                payer_user=user,
                channel=channel,
                card_reference_id=card_reference_id,
                idempotency_key=idempotency_key,
                extra=extra,
            )
            return payment, client_secret, None
        except HTTPException as e:
            session.rollback()
            return None, None, e.detail
        except Exception as e:
            session.rollback()
            return None, None, f"Bill payment failed: {e}"

    def mark_bill_paid_offline(
        self,
        session: Session,
        bill_id: int,
        driver_id: int,
        note: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Driver confirms cash collected from the user. Settles immediately on
        the central ledger (cash collected offline → succeeded right away)."""
        from app.modules.payments import service as central
        try:
            bill = session.exec(
                select(TripBill).where(TripBill.id == bill_id).with_for_update()
            ).first()
            if not bill:
                return False, "Bill not found"
            if bill.driver_id != driver_id:
                return False, "Not authorized for this bill"
            if bill.is_paid:
                return False, "Bill already paid"
            trip = session.get(Trip, bill.trip_id)
            if not trip:
                return False, "Trip not found"
            payer_user = session.get(User, bill.user_id)
            if not payer_user:
                return False, "Bill user not found"

            extra = {"bill_id": bill.id}
            if note:
                extra["note"] = note
            central.create_trip_payment_intent(
                session,
                trip=trip,
                purpose=_bill_purpose(bill),
                amount=bill.total_amount,
                payer_type="user",
                payer_user=payer_user,
                channel="cash",
                extra=extra,
            )
            return True, None
        except HTTPException as e:
            session.rollback()
            return False, e.detail
        except Exception as e:
            session.rollback()
            return False, f"Mark-paid failed: {e}"

    def user_has_outstanding_dues(
        self, session: Session, user_id
    ) -> Tuple[bool, Optional[str]]:
        """Booking gate: ``(has_dues, kind)`` where kind ∈ {"bill","settlement",None}.

        Picks up daily bills, cancellation_balance shortfalls, and unpaid
        final settlements. ``kind`` lets the caller render a precise message.
        """
        bill = session.exec(
            select(TripBill).where(
                TripBill.user_id == user_id,
                # F6: include cancellation_balance shortfall bills in the
                # booking gate too. Either kind unpaid blocks new bookings.
                TripBill.bill_type.in_(("daily_bill", "cancellation_balance")),
                TripBill.amount_due > 0,
            )
        ).first()
        if bill is not None:
            return True, "bill"

        settlement = session.exec(
            select(TripSettlement).where(
                TripSettlement.user_id == user_id,
                TripSettlement.user_payment_status != "paid",
                TripSettlement.remaining_due > 0,
            )
        ).first()
        if settlement is not None:
            return True, "settlement"
        return False, None

    def user_has_unpaid_bills(self, session: Session, user_id) -> bool:
        """Boolean shim over :meth:`user_has_outstanding_dues` for legacy callers."""
        has, _ = self.user_has_outstanding_dues(session, user_id)
        return has

    def trip_has_unpaid_bills(self, session: Session, trip_id: int) -> bool:
        """True iff the trip has any open-balance bill that blocks progression.

        Checks ``amount_due > 0`` (not just ``is_paid == False``) so
        advance/full-payment trips whose daily bills carry zero due don't
        accidentally pause. ``schedule_diff`` bills also block — the user must
        pay the modification top-up before the new schedule's first OTP.
        """
        row = session.exec(
            select(TripBill).where(
                TripBill.trip_id == trip_id,
                TripBill.bill_type.in_(("daily_bill", "schedule_diff")),
                TripBill.amount_due > 0,
            )
        ).first()
        return row is not None

    def unpause_trip_if_clear(self, session: Session, trip_id: int) -> None:
        """After a bill is paid, lift OTP block and flip `paused` → `active_pending_otp`."""
        if self.trip_has_unpaid_bills(session, trip_id):
            return

        paused_atts = session.exec(
            select(TripAttendance).where(
                TripAttendance.trip_id == trip_id,
                TripAttendance.status == "paused_payment",
            )
        ).all()
        for att in paused_atts:
            att.status = "scheduled"
            att.skip_reason = None
            att.marked_by = "system"
            session.add(att)

        # Lock the trip row so we don't race with end-trip / auto-end / skip.
        trip = session.exec(
            select(Trip).where(Trip.id == trip_id).with_for_update()
        ).first()
        if not trip:
            return
        changed = False
        if trip.is_payment_blocked:
            trip.is_payment_blocked = False
            changed = True
        if trip.status == "paused":
            # Only re-arm OTP if there's actually a pending shift to run.
            pending = session.exec(
                select(TripAttendance).where(
                    TripAttendance.trip_id == trip_id,
                    TripAttendance.status.in_(["scheduled", "paused_payment"]),
                )
            ).first()
            if pending:
                trip.status = "active_pending_otp"
            else:
                trip.status = "completed"
            changed = True
        if changed:
            trip.state_version += 1
            session.add(trip)

    def process_refund(
        self,
        session: Session,
        trip_id: int,
        refund_amount: float,
        reason: str = "Trip cancelled",
    ) -> Tuple[bool, Optional[str]]:
        """Refund the user a computed amount, allocated across their succeeded
        trip charges via the central partial-refund path (wallet→wallet,
        platform→source). The caller commits the trip status afterwards."""
        from app.modules.trips import payment_orchestrator

        try:
            if refund_amount <= 0:
                return True, None

            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            payment_orchestrator.refund_trip_amount(
                session,
                trip_id,
                round(refund_amount, 2),
                reason,
                actor="system",
                actor_id=str(trip.user_id),
            )
            return True, None
        except HTTPException as e:
            session.rollback()
            return False, e.detail
        except Exception as e:
            session.rollback()
            return False, f"Refund processing failed: {str(e)}"
