"""
Payment service for trip flow.

Wraps the (dummy) payment gateway with the rules that make trip billing safe:
row-level locks on bills + settlements, gateway-style refund accounting,
audit emission on every money-touching commit, and a single funnel through
which both user and driver transactions flow.
"""

from app.utils.time_utils import now_ist
from typing import Optional, Tuple
from sqlmodel import Session, select
import redis
import uuid

from app.core.models import (
    Trip,
    PaymentTransaction,
    TripAttendance,
    TripBill,
    TripSettlement,
    SystemConfig,
)
from app.services.audit_log import emit_event as audit_emit

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

    def process_dummy_payment(
        self,
        amount: float,
        payer_id: str,
        payer_type: str,  # "user" or "driver"
        payment_method: str = "card",
    ) -> Tuple[bool, str]:
        """Mock payment gateway. Replace with Razorpay/Stripe behind the same shape.

        Returns ``(success, transaction_id_or_error)``. Non-positive amounts
        are rejected so a misconfigured caller surfaces fast.
        """
        if amount <= 0:
            return False, "Invalid amount"
        transaction_id = f"TXN_{payer_type}_{payer_id}_{uuid.uuid4().hex[:8]}"
        return True, transaction_id

    def process_dummy_refund(
        self,
        original_gateway_transaction_id: Optional[str],
        amount: float,
        payer_type: str,
    ) -> Tuple[bool, Optional[str]]:
        """Mock the gateway refund API call (F9).

        Real gateways (Razorpay/Stripe) accept the original charge id + amount
        and return a refund id. We mirror that shape so the caller code is
        gateway-agnostic. Today every non-zero refund succeeds with a synthetic
        id; tomorrow this gets swapped for the real client without changing
        :meth:`process_refund` or :meth:`refund_driver_acceptance_fee`.

        Returns ``(success, gateway_refund_id_or_error)``.
        """
        if amount <= 0:
            return False, "Invalid refund amount"
        # When the original charge has no gateway id we still issue a refund id
        # so the audit trail records the attempt; a real adapter would 4xx here.
        suffix = uuid.uuid4().hex[:10]
        refund_id = f"RFND_{payer_type}_{suffix}"
        if original_gateway_transaction_id:
            refund_id = f"{refund_id}_for_{original_gateway_transaction_id[:18]}"
        return True, refund_id

    def driver_accept_payment(
        self, session: Session, trip_id: int, driver_id: int
    ) -> Tuple[bool, Optional[str]]:
        """Driver pays the acceptance fee to lock the trip."""
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            if trip.driver_id != driver_id:
                return False, "Driver not matched to this trip"

            if trip.driver_payment_status != "unpaid":
                return False, f"Driver payment already {trip.driver_payment_status}"

            fee = get_driver_acceptance_fee(session, self.redis)

            success, txn_id = self.process_dummy_payment(
                amount=fee,
                payer_id=str(driver_id),
                payer_type="driver",
            )
            if not success:
                return False, f"Payment failed: {txn_id}"

            payment_txn = PaymentTransaction(
                trip_id=trip_id,
                driver_id=driver_id,
                payer_type="driver",
                payment_type="driver_acceptance",
                amount=fee,
                payment_status="success",
                payment_method="card",
                gateway_transaction_id=txn_id,
                completed_at=now_ist(),
            )
            session.add(payment_txn)

            trip.driver_payment_status = "paid"
            trip.driver_payment_amount = fee
            session.add(trip)
            session.commit()

            audit_emit(
                "payment.driver_acceptance",
                trip_id=trip_id,
                actor="driver",
                actor_id=str(driver_id),
                payload={
                    "amount": fee,
                    "gateway_transaction_id": txn_id,
                    "payment_method": "card",
                },
            )
            return True, None

        except Exception as e:
            return False, f"Driver payment processing failed: {str(e)}"

    def user_make_payment(
        self,
        session: Session,
        trip_id: int,
        user_id: str,
        amount: float,
        payment_method: str = "card",
    ) -> Tuple[bool, Optional[str]]:
        """User upfront / advance / settlement payment."""
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            if str(trip.user_id) != user_id:
                return False, "User not matched to this trip"

            success, txn_id = self.process_dummy_payment(
                amount=amount,
                payer_id=user_id,
                payer_type="user",
                payment_method=payment_method,
            )
            if not success:
                return False, f"Payment failed: {txn_id}"

            payment_txn = PaymentTransaction(
                trip_id=trip_id,
                user_id=trip.user_id,
                payer_type="user",
                payment_type=trip.payment_method or "trip_day",
                amount=amount,
                payment_status="success",
                payment_method=payment_method,
                gateway_transaction_id=txn_id,
                completed_at=now_ist(),
            )
            session.add(payment_txn)
            session.commit()

            audit_emit(
                "payment.user_upfront",
                trip_id=trip_id,
                actor="user",
                actor_id=str(user_id),
                payload={
                    "amount": amount,
                    "payment_type": trip.payment_method or "trip_day",
                    "payment_method": payment_method,
                    "gateway_transaction_id": txn_id,
                },
            )
            return True, None

        except Exception as e:
            return False, f"User payment processing failed: {str(e)}"

    def calculate_driver_fee_refund(
        self, session: Session, trip_id: int, driver_id: Optional[int]
    ) -> float:
        """Acceptance fee paid (and not yet refunded) for this driver on this trip."""
        if not driver_id:
            return 0.0
        paid = session.exec(
            select(PaymentTransaction).where(
                PaymentTransaction.trip_id == trip_id,
                PaymentTransaction.driver_id == driver_id,
                PaymentTransaction.payer_type == "driver",
                PaymentTransaction.payment_type == "driver_acceptance",
                PaymentTransaction.payment_status == "success",
            )
        ).first()
        if not paid:
            return 0.0
        already = session.exec(
            select(PaymentTransaction).where(
                PaymentTransaction.trip_id == trip_id,
                PaymentTransaction.driver_id == driver_id,
                PaymentTransaction.payer_type == "driver",
                PaymentTransaction.payment_status == "refunded",
            )
        ).first()
        if already:
            return 0.0
        return float(paid.amount)

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

        upfront_paid = sum(
            txn.amount
            for txn in session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "user",
                    PaymentTransaction.payment_status == "success",
                )
            ).all()
        )

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

        total_paid = sum(
            txn.amount
            for txn in session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "user",
                    PaymentTransaction.payment_status == "success",
                )
            ).all()
        )
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

            total_paid = sum(
                txn.amount
                for txn in session.exec(
                    select(PaymentTransaction).where(
                        PaymentTransaction.trip_id == trip_id,
                        PaymentTransaction.payer_type == "user",
                        PaymentTransaction.payment_status == "success",
                    )
                ).all()
            )

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
        """Refund the acceptance fee. No-ops if never paid or already refunded."""
        try:
            paid = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.driver_id == driver_id,
                    PaymentTransaction.payer_type == "driver",
                    PaymentTransaction.payment_type == "driver_acceptance",
                    PaymentTransaction.payment_status == "success",
                )
            ).first()
            if not paid:
                return True, None

            already = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.driver_id == driver_id,
                    PaymentTransaction.payment_status == "refunded",
                )
            ).first()
            if already:
                return True, None

            gateway_ok, gateway_refund_id = self.process_dummy_refund(
                original_gateway_transaction_id=paid.gateway_transaction_id,
                amount=paid.amount,
                payer_type="driver",
            )
            refund_txn = PaymentTransaction(
                trip_id=trip_id,
                driver_id=driver_id,
                payer_type="driver",
                payment_type="driver_acceptance",
                amount=paid.amount,
                payment_status="refunded" if gateway_ok else "refund_failed",
                payment_method="refund",
                gateway_transaction_id=paid.gateway_transaction_id,
                gateway_refund_id=gateway_refund_id if gateway_ok else None,
                refund_reason=(
                    reason
                    if gateway_ok
                    else f"{reason} (gateway error: {gateway_refund_id})"
                ),
                refund_at=now_ist(),
                refund_amount=paid.amount,
            )
            session.add(refund_txn)
            session.commit()
            audit_emit(
                "refund.driver_acceptance",
                trip_id=trip_id,
                actor="system",
                actor_id=str(driver_id),
                severity="info" if gateway_ok else "warning",
                payload={
                    "amount": paid.amount,
                    "reason": reason,
                    "gateway_refund_id": gateway_refund_id if gateway_ok else None,
                    "gateway_status": "success" if gateway_ok else "failed",
                },
            )
            if not gateway_ok:
                return False, f"Gateway refund failed: {gateway_refund_id}"
            return True, None
        except Exception as e:
            session.rollback()
            return False, f"Driver fee refund failed: {e}"

    def _settle_bill_row(
        self,
        session: Session,
        bill: TripBill,
        paid_by: str,  # "user_online" | "driver_offline"
        paid_by_driver_id: Optional[int] = None,
        gateway_txn_id: Optional[str] = None,
        payment_method: str = "card",
        note: Optional[str] = None,
    ) -> None:
        bill.amount_paid = bill.total_amount
        bill.amount_due = 0.0
        bill.is_paid = True
        bill.paid_at = now_ist()
        bill.paid_by = paid_by
        bill.paid_by_driver_id = paid_by_driver_id
        if note:
            bill.payment_note = note
        session.add(bill)

        session.add(
            PaymentTransaction(
                trip_id=bill.trip_id,
                user_id=bill.user_id,
                payer_type="user",
                payment_type="trip_day_bill",
                amount=bill.total_amount,
                payment_status="success",
                payment_method=payment_method if paid_by == "user_online" else "cash",
                gateway_transaction_id=gateway_txn_id,
                completed_at=now_ist(),
            )
        )
        audit_emit(
            "payment.daily_bill",
            trip_id=bill.trip_id,
            actor="user" if paid_by == "user_online" else "driver",
            actor_id=str(bill.user_id)
            if paid_by == "user_online"
            else str(paid_by_driver_id),
            payload={
                "bill_id": bill.id,
                "amount": bill.total_amount,
                "paid_by": paid_by,
                "payment_method": payment_method
                if paid_by == "user_online"
                else "cash",
                "gateway_transaction_id": gateway_txn_id,
            },
        )

    def pay_bill_online(
        self,
        session: Session,
        bill_id: int,
        user_id,
        payment_method: str = "card",
        note: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """User pays a bill via the (dummy) gateway. Row-locked for safety."""
        try:
            bill = session.exec(
                select(TripBill).where(TripBill.id == bill_id).with_for_update()
            ).first()

            if not bill:
                return False, "Bill not found"
            if str(bill.user_id) != str(user_id):
                return False, "Not authorized for this bill"
            if bill.is_paid:
                return False, "Bill already paid"

            success, txn_id = self.process_dummy_payment(
                amount=bill.total_amount,
                payer_id=str(user_id),
                payer_type="user",
                payment_method=payment_method,
            )
            if not success:
                return False, f"Payment failed: {txn_id}"

            self._settle_bill_row(
                session,
                bill,
                paid_by="user_online",
                gateway_txn_id=txn_id,
                payment_method=payment_method,
                note=note,
            )
            self.unpause_trip_if_clear(session, bill.trip_id)
            session.commit()
            return True, None
        except Exception as e:
            session.rollback()
            return False, f"Bill payment failed: {e}"

    def mark_bill_paid_offline(
        self,
        session: Session,
        bill_id: int,
        driver_id: int,
        note: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Driver confirms cash collected from user. Row-locked for safety."""
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

            self._settle_bill_row(
                session,
                bill,
                paid_by="driver_offline",
                paid_by_driver_id=driver_id,
                payment_method="cash",
                note=note,
            )
            self.unpause_trip_if_clear(session, bill.trip_id)
            session.commit()
            return True, None
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
        """Refund the user via the gateway. Stores a ``refunded`` (or
        ``refund_failed``) PaymentTransaction row + emits an audit event.
        Caller commits trip status after; this method commits the refund row.
        """
        try:
            if refund_amount <= 0:
                return True, None

            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            # Most gateway refund APIs need the original charge id to anchor the
            # refund — pick the most recent successful user payment for that.
            original_charge = session.exec(
                select(PaymentTransaction)
                .where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "user",
                    PaymentTransaction.payment_status == "success",
                )
                .order_by(PaymentTransaction.id.desc())
            ).first()

            gateway_ok, gateway_refund_id = self.process_dummy_refund(
                original_gateway_transaction_id=(
                    original_charge.gateway_transaction_id if original_charge else None
                ),
                amount=refund_amount,
                payer_type="user",
            )

            refund_txn = PaymentTransaction(
                trip_id=trip_id,
                user_id=trip.user_id,
                payer_type="user",
                payment_type=trip.payment_method or "trip_day",
                amount=refund_amount,
                payment_status="refunded" if gateway_ok else "refund_failed",
                payment_method="refund",
                gateway_transaction_id=(
                    original_charge.gateway_transaction_id if original_charge else None
                ),
                gateway_refund_id=gateway_refund_id if gateway_ok else None,
                refund_reason=(
                    reason
                    if gateway_ok
                    else f"{reason} (gateway error: {gateway_refund_id})"
                ),
                refund_at=now_ist(),
                refund_amount=refund_amount,
            )
            session.add(refund_txn)
            session.commit()

            audit_emit(
                "refund.user",
                trip_id=trip_id,
                actor="system",
                actor_id=str(trip.user_id),
                severity="info" if gateway_ok else "warning",
                payload={
                    "amount": refund_amount,
                    "reason": reason,
                    "payment_method": trip.payment_method or "trip_day",
                    "gateway_refund_id": gateway_refund_id if gateway_ok else None,
                    "gateway_status": "success" if gateway_ok else "failed",
                },
            )
            if not gateway_ok:
                # Failed row left in place for ops retry / reconciliation.
                return False, f"Gateway refund failed: {gateway_refund_id}"
            return True, None

        except Exception as e:
            return False, f"Refund processing failed: {str(e)}"
