"""
Payment Service for Trip Management
Handles driver payment, user payment, and refunds
"""

from datetime import datetime
from typing import Optional, Tuple
from sqlmodel import Session, select
import redis

from app.core.models import (
    Trip,
    PaymentTransaction,
    TripAttendance,
    TripBill,
    SystemConfig,
)

# Key in SystemConfig table (admin-editable via /admin/system-config).
DRIVER_ACCEPTANCE_FEE_KEY = "driver_acceptance_fee"
DEFAULT_DRIVER_ACCEPTANCE_FEE = 100.0


def get_driver_acceptance_fee(
    session: Session,
    redis_client: Optional[redis.Redis] = None,
) -> float:
    """
    Resolve the driver acceptance fee at request time.
    Lookup order: Redis cache  ->  SystemConfig DB row  ->  hard-coded default.
    Mirrors how pricing_algo.py resolves base_fare/rate_per_km.
    """
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
        """
        Process dummy payment (mock payment gateway)

        In production, this would integrate with Razorpay/Stripe
        For now, all payments succeed except for specific test amounts

        Returns:
            Tuple of (success, transaction_id or error_message)
        """
        # Dummy logic: payments fail if amount is 0 or negative
        if amount <= 0:
            return False, "Invalid amount"

        # Dummy transaction ID
        transaction_id = (
            f"TXN_{payer_type}_{payer_id}_{int(datetime.utcnow().timestamp())}"
        )

        # In real scenario, this would call payment gateway API
        # For now, simulate success
        success = True

        return success, transaction_id

    def driver_accept_payment(
        self, session: Session, trip_id: int, driver_id: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Driver pays fixed amount to accept and finalize trip

        Args:
            session: Database session
            trip_id: Trip ID
            driver_id: Driver ID

        Returns:
            Tuple of (success, error_message)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            if trip.driver_id != driver_id:
                return False, "Driver not matched to this trip"

            if trip.driver_payment_status != "unpaid":
                return False, f"Driver payment already {trip.driver_payment_status}"

            # Resolve the configured fee at the moment of the transaction
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
                completed_at=datetime.utcnow(),
            )
            session.add(payment_txn)

            trip.driver_payment_status = "paid"
            trip.driver_payment_amount = fee
            session.add(trip)
            session.commit()

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
        """
        User makes payment for trip

        Args:
            session: Database session
            trip_id: Trip ID
            user_id: User ID
            amount: Amount to pay
            payment_method: Payment method

        Returns:
            Tuple of (success, error_message)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            if str(trip.user_id) != user_id:
                return False, "User not matched to this trip"

            # Process dummy payment
            success, txn_id = self.process_dummy_payment(
                amount=amount,
                payer_id=user_id,
                payer_type="user",
                payment_method=payment_method,
            )

            if not success:
                return False, f"Payment failed: {txn_id}"

            # Record payment transaction
            payment_txn = PaymentTransaction(
                trip_id=trip_id,
                user_id=trip.user_id,
                payer_type="user",
                payment_type=trip.payment_method or "trip_day",
                amount=amount,
                payment_status="success",
                payment_method=payment_method,
                gateway_transaction_id=txn_id,
                completed_at=datetime.utcnow(),
            )
            session.add(payment_txn)
            session.commit()

            return True, None

        except Exception as e:
            return False, f"User payment processing failed: {str(e)}"

    def calculate_refund_amount(
        self, session: Session, trip_id: int, cancellation_reason: Optional[str] = None
    ) -> Tuple[float, Optional[str]]:
        """
        Calculate refund amount based on payment method and trip status

        Args:
            session: Database session
            trip_id: Trip ID
            cancellation_reason: Why trip is being cancelled

        Returns:
            Tuple of (refund_amount, error_message)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return 0.0, "Trip not found"

            # Get total paid by user
            paid_transactions = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "user",
                    PaymentTransaction.payment_status == "success",
                )
            ).all()

            total_paid = sum(txn.amount for txn in paid_transactions)

            # Refund logic based on payment method and trip state
            if trip.payment_method == "trip_day":
                # Full refund if cancelled before trip starts
                if trip.actual_start_time is None:
                    return total_paid, None
                else:
                    # No refund if trip already started
                    return 0.0, None

            elif trip.payment_method == "advance_20":
                # Refund only if no trips completed
                completed_attendances = session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == trip_id,
                        TripAttendance.status == "present",
                    )
                ).all()

                if len(completed_attendances) == 0:
                    return total_paid, None
                else:
                    return 0.0, None

            elif trip.payment_method == "full_payment":
                # Partial refund: total - (days_used * daily_rate)
                if trip.fare and trip.trip_duration_hours:
                    daily_rate = (
                        trip.fare / ((trip.end_date - trip.start_date).days + 1)
                        if trip.end_date and trip.start_date
                        else trip.fare
                    )

                    completed_attendances = session.exec(
                        select(TripAttendance).where(
                            TripAttendance.trip_id == trip_id,
                            TripAttendance.status == "present",
                        )
                    ).all()

                    days_used = len(completed_attendances)
                    used_amount = daily_rate * days_used
                    refund = max(0, total_paid - used_amount)
                    return refund, None
                else:
                    return 0.0, None

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
        """Refund the fixed driver-acceptance fee (if it was actually paid)."""
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
                return True, None  # nothing to refund

            # Idempotency: don't double-refund
            already = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.driver_id == driver_id,
                    PaymentTransaction.payment_status == "refunded",
                )
            ).first()
            if already:
                return True, None

            refund_txn = PaymentTransaction(
                trip_id=trip_id,
                driver_id=driver_id,
                payer_type="driver",
                payment_type="driver_acceptance",
                amount=paid.amount,
                payment_status="refunded",
                payment_method="refund",
                refund_reason=reason,
                refund_at=datetime.utcnow(),
                refund_amount=paid.amount,
            )
            session.add(refund_txn)
            session.commit()
            return True, None
        except Exception as e:
            session.rollback()
            return False, f"Driver fee refund failed: {e}"

    # ─────────────────────────────────────────────────────────────
    # Daily-bill settlement helpers
    # ─────────────────────────────────────────────────────────────
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
        bill.paid_at = datetime.utcnow()
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
                completed_at=datetime.utcnow(),
            )
        )

    def pay_bill_online(
        self,
        session: Session,
        bill_id: int,
        user_id,
        payment_method: str = "card",
    ) -> Tuple[bool, Optional[str]]:
        """User pays a daily bill via the dummy gateway."""
        try:
            bill = session.get(TripBill, bill_id)
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
        """Driver confirms cash collected from user (offline payment)."""
        try:
            bill = session.get(TripBill, bill_id)
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

    def user_has_unpaid_bills(self, session: Session, user_id) -> bool:
        """Any unpaid daily_bill anywhere on the user's account?"""
        row = session.exec(
            select(TripBill).where(
                TripBill.user_id == user_id,
                TripBill.bill_type == "daily_bill",
                TripBill.is_paid == False,  # noqa: E712
            )
        ).first()
        return row is not None

    def trip_has_unpaid_bills(self, session: Session, trip_id: int) -> bool:
        row = session.exec(
            select(TripBill).where(
                TripBill.trip_id == trip_id,
                TripBill.bill_type == "daily_bill",
                TripBill.is_paid == False,  # noqa: E712
            )
        ).first()
        return row is not None

    def unpause_trip_if_clear(self, session: Session, trip_id: int) -> None:
        """If a trip's last unpaid bill was just cleared, lift the OTP-generation block."""
        if self.trip_has_unpaid_bills(session, trip_id):
            return
        trip = session.get(Trip, trip_id)
        if trip and trip.is_payment_blocked:
            trip.is_payment_blocked = False
            session.add(trip)

    def process_refund(
        self,
        session: Session,
        trip_id: int,
        refund_amount: float,
        reason: str = "Trip cancelled",
    ) -> Tuple[bool, Optional[str]]:
        """
        Process refund to user

        Args:
            session: Database session
            trip_id: Trip ID
            refund_amount: Amount to refund
            reason: Reason for refund

        Returns:
            Tuple of (success, error_message)
        """
        try:
            if refund_amount <= 0:
                return True, None  # No refund needed

            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            # In production, this would process refund via payment gateway
            # For now, just record the transaction
            refund_txn = PaymentTransaction(
                trip_id=trip_id,
                user_id=trip.user_id,
                payer_type="user",
                payment_type=trip.payment_method or "trip_day",
                amount=refund_amount,
                payment_status="refunded",
                payment_method="refund",
                refund_reason=reason,
                refund_at=datetime.utcnow(),
            )
            session.add(refund_txn)
            session.commit()

            return True, None

        except Exception as e:
            return False, f"Refund processing failed: {str(e)}"
