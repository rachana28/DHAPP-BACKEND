"""
Daily-bill generation and final settlement for trips.

Daily bills exist only for ``trip_day``; advance/full-payment settle through
upfront credit pools and the final settlement row. The payment-method
discount is baked into the daily bill total so the user-visible amount is
the actual amount due, not gross.
"""

import logging
import math
from datetime import date

from app.utils.notifications import notify_safe
from app.utils.time_utils import today_ist, now_ist
from typing import Optional, Tuple, Dict, Any
from sqlmodel import Session, select

from app.core.models import (
    Trip,
    Driver,
    TripAttendance,
    Payment,
    TripBill,
    TripSettlement,
    PricingComponentBreakdown,
)
from app.services.audit_log import emit_event as audit_emit

logger = logging.getLogger(__name__)

_PAID_IN_STATES = ["succeeded", "partially_refunded"]

# Flat fee a user forfeits for each day THEY voluntarily skip on a prepaid trip
# (mirrors the cancellation anti-fraud fee). Driver-fault skips never incur it.
USER_SKIP_FEE = 50.0


def payment_method_discount_pct(
    hiring_type: Optional[str], payment_method: Optional[str]
) -> float:
    if (hiring_type or "").strip().lower() == "outstation":
        return 3.0
    if payment_method == "full_payment":
        return 5.0
    if payment_method == "advance_20":
        return 2.0
    return 0.0


class BillingService:
    def calculate_daily_bill_components(
        self, session: Session, trip_id: int, trip_date: date
    ) -> Tuple[Dict[str, float], float, Optional[str]]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return {}, 0.0, "Trip not found"

            attendances = session.exec(
                select(TripAttendance).where(TripAttendance.trip_id == trip_id)
            ).all()

            num_days = len(attendances)
            if num_days == 0:
                return {}, 0.0, "No attendance records found for trip"

            if trip.fare and trip.fare_breakdown:
                daily_total = trip.fare / num_days

                breakdown = trip.fare_breakdown
                comp_list = None
                if isinstance(breakdown, dict):
                    raw = breakdown.get("components")
                    if isinstance(raw, list):
                        comp_list = raw
                elif isinstance(breakdown, list):
                    comp_list = breakdown

                components: Dict[str, float] = {}
                if comp_list:
                    for comp in comp_list:
                        if (
                            isinstance(comp, dict)
                            and "name" in comp
                            and "amount" in comp
                        ):
                            try:
                                amt = float(comp["amount"])
                            except (TypeError, ValueError):
                                continue
                            daily_amount = amt / num_days if num_days > 0 else 0
                            components[comp["name"]] = daily_amount

                if not components:
                    components["Base Fare"] = daily_total

                return components, daily_total, None

            if trip.fare:
                daily_fare = trip.fare / num_days
                components = {"Base Fare": daily_fare}
                return components, daily_fare, None

            return {}, 0.0, "Trip fare not calculated"

        except Exception as e:
            return {}, 0.0, f"Bill calculation failed: {str(e)}"

    def generate_daily_bill(
        self, session: Session, trip_id: int, trip_date: date
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, None, "Trip not found"

            if trip.payment_method in ("advance_20", "full_payment"):
                return True, None, None

            existing_bill = session.exec(
                select(TripBill).where(
                    TripBill.trip_id == trip_id,
                    TripBill.bill_type == "daily_bill",
                    TripBill.bill_date == trip_date,
                )
            ).first()

            if existing_bill:
                return False, existing_bill.id, "Bill already exists for this day"

            components, gross_amount, error = self.calculate_daily_bill_components(
                session, trip_id, trip_date
            )

            if error:
                return False, None, error

            discount_pct = payment_method_discount_pct(
                trip.hiring_type, trip.payment_method
            )
            discount_amount = round(gross_amount * discount_pct / 100.0, 2)
            total_amount = round(gross_amount - discount_amount, 2)

            upfront_payments = session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_type == "user",
                    Payment.status.in_(_PAID_IN_STATES),
                    Payment.purpose.notin_(["daily_bill", "settlement"]),
                )
            ).all()
            total_upfront = sum(p.amount for p in upfront_payments)

            prior_daily_bills = session.exec(
                select(TripBill).where(
                    TripBill.trip_id == trip_id,
                    TripBill.bill_type == "daily_bill",
                )
            ).all()
            trip_day_cash = sum(
                p.amount
                for p in session.exec(
                    select(Payment).where(
                        Payment.service_type == "trip",
                        Payment.service_id == trip_id,
                        Payment.payer_type == "user",
                        Payment.status.in_(_PAID_IN_STATES),
                        Payment.purpose == "daily_bill",
                    )
                ).all()
            )
            already_allocated = max(
                0.0,
                sum(b.amount_paid for b in prior_daily_bills) - trip_day_cash,
            )

            credit_available = max(0.0, total_upfront - already_allocated)
            amount_paid = min(total_amount, credit_available)
            amount_due = max(0.0, total_amount - amount_paid)

            is_paid = amount_due == 0

            components_list = [
                {
                    "name": name,
                    "amount": amount,
                    "percentage": (amount / gross_amount * 100)
                    if gross_amount > 0
                    else 0.0,
                }
                for name, amount in components.items()
            ]
            if discount_amount > 0:
                components_list.append(
                    {
                        "name": f"Payment Discount ({discount_pct:.0f}%)",
                        "amount": -discount_amount,
                        "percentage": -discount_pct,
                    }
                )

            bill = TripBill(
                trip_id=trip_id,
                user_id=trip.user_id,
                driver_id=trip.driver_id,
                bill_type="daily_bill",
                bill_date=trip_date,
                total_amount=total_amount,
                amount_paid=amount_paid,
                amount_due=amount_due,
                discount_percentage=discount_pct or None,
                discount_amount=discount_amount,
                is_paid=is_paid,
                paid_at=now_ist() if is_paid else None,
                is_generated=True,
                components=components_list,
            )
            session.add(bill)
            session.flush()

            for entry in components_list:
                pricing_component = PricingComponentBreakdown(
                    trip_id=trip_id,
                    bill_id=bill.id,
                    component_name=entry["name"],
                    amount=entry["amount"],
                    percentage=entry["percentage"],
                    trip_date=trip_date,
                )
                session.add(pricing_component)

            session.commit()
            session.refresh(bill)

            return True, bill.id, None

        except Exception as e:
            return False, None, f"Daily bill generation failed: {str(e)}"

    def check_advance_recovery(self, session: Session, trip_id: int) -> Optional[int]:
        trip = session.get(Trip, trip_id)
        if not trip or trip.payment_method != "advance_20":
            return None
        if trip.is_payment_blocked or not trip.driver_id:
            return None
        if trip.status not in ("active_pending_otp", "active", "ongoing", "paused"):
            return None

        existing = session.exec(
            select(TripBill).where(
                TripBill.trip_id == trip_id,
                TripBill.bill_type == "advance_recovery",
            )
        ).first()
        if existing:
            return existing.id

        attendances = session.exec(
            select(TripAttendance).where(TripAttendance.trip_id == trip_id)
        ).all()
        total = len(attendances)
        if total <= 1:
            return None
        if not any(a.status in ("scheduled", "paused_payment") for a in attendances):
            return None
        concluded = sum(
            1
            for a in attendances
            if a.status
            in ("present", "skipped_by_user", "skipped_by_driver", "skipped_by_system")
        )
        if concluded < math.ceil(total / 2):
            return None

        fare = float(trip.fare or 0.0)
        if fare <= 0:
            return None
        discount_pct = payment_method_discount_pct(
            trip.hiring_type, trip.payment_method
        )
        per_day_net = (fare / total) * (1 - discount_pct / 100.0)
        expected_net_total = round(per_day_net * total, 2)

        total_user_paid = sum(
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
        remaining = round(expected_net_total - total_user_paid, 2)
        amount = round(remaining / 2.0, 2)
        if amount <= 0:
            return None

        bill = TripBill(
            trip_id=trip_id,
            user_id=trip.user_id,
            driver_id=trip.driver_id,
            bill_type="advance_recovery",
            bill_date=today_ist(),
            total_amount=amount,
            amount_paid=0.0,
            amount_due=amount,
            discount_percentage=discount_pct or None,
            discount_amount=0.0,
            is_generated=True,
            is_paid=False,
            notes=(
                f"Mid-trip recovery: half of the remaining balance after "
                f"{concluded}/{total} scheduled days concluded."
            ),
            components=[
                {
                    "name": "Mid-trip Recovery (half of remaining balance)",
                    "amount": amount,
                    "percentage": 100.0,
                }
            ],
        )
        session.add(bill)
        session.commit()
        session.refresh(bill)

        notify_safe(
            session,
            [trip.user_id],
            "Mid-trip payment due",
            (
                f"Half of your trip's remaining balance (₹{amount:.2f}) is now "
                f"due. Pay it to keep your upcoming shifts active."
            ),
            {
                "type": "advance_recovery_bill",
                "trip_id": trip.id,
                "bill_id": bill.id,
                "amount": amount,
            },
        )
        return bill.id

    def generate_final_settlement(
        self, session: Session, trip_id: int
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, None, "Trip not found"

            session.exec(
                select(Trip).where(Trip.id == trip_id).with_for_update()
            ).first()

            existing_settlement = session.exec(
                select(TripSettlement).where(TripSettlement.trip_id == trip_id)
            ).first()

            if existing_settlement:
                return (
                    False,
                    existing_settlement.id,
                    "Settlement already exists for this trip",
                )

            attendances = session.exec(
                select(TripAttendance).where(TripAttendance.trip_id == trip_id)
            ).all()

            present_count = len([a for a in attendances if a.status == "present"])
            absent_count = len([a for a in attendances if "skipped" in a.status])
            total_trips = len(attendances)

            daily_bills = session.exec(
                select(TripBill).where(
                    TripBill.trip_id == trip_id, TripBill.bill_type == "daily_bill"
                )
            ).all()

            total_earned = sum(bill.total_amount for bill in daily_bills)

            billed_dates = {b.bill_date for b in daily_bills}
            unbilled_present = [
                a
                for a in attendances
                if a.status == "present" and a.trip_date not in billed_dates
            ]
            if unbilled_present and trip.fare and total_trips > 0:
                per_day_gross = trip.fare / total_trips
                discount_pct = payment_method_discount_pct(
                    trip.hiring_type, trip.payment_method
                )
                per_day_net = per_day_gross * (1 - discount_pct / 100.0)
                total_earned += per_day_net * len(unbilled_present)

            user_payments = session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_type == "user",
                    Payment.status.in_(_PAID_IN_STATES),
                )
            ).all()

            driver_payments = session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_type == "driver",
                    Payment.status.in_(_PAID_IN_STATES),
                )
            ).all()

            total_user_paid = sum(p.amount for p in user_payments)
            total_driver_paid = sum(p.amount for p in driver_payments)

            final_earned = round(total_earned, 2)

            # Prepaid methods (advance_20 / full_payment) have no daily bills, so
            # settle against an attribution-based "basis" instead: present days are
            # retained at the NET (discounted) rate, user-skipped days forfeit only
            # the flat USER_SKIP_FEE, and driver-fault days (skipped_by_driver /
            # skipped_by_system) are credited the discount back so the user is made
            # whole at the GROSS rate on a day the driver failed to serve. Clamped
            # at >= 0 so a heavily driver-skipped trip refunds at most what was paid.
            if (
                trip.payment_method in ("advance_20", "full_payment")
                and trip.fare
                and total_trips > 0
            ):
                per_day_gross = trip.fare / total_trips
                discount_pct = payment_method_discount_pct(
                    trip.hiring_type, trip.payment_method
                )
                per_day_net = per_day_gross * (1 - discount_pct / 100.0)
                driver_fault_days = len(
                    [
                        a
                        for a in attendances
                        if a.status in ("skipped_by_driver", "skipped_by_system")
                    ]
                )
                user_skip_days = len(
                    [a for a in attendances if a.status == "skipped_by_user"]
                )
                basis = (
                    present_count * per_day_net
                    + user_skip_days * USER_SKIP_FEE
                    - driver_fault_days * (per_day_gross - per_day_net)
                )
                final_earned = round(max(0.0, basis), 2)

            remaining_due = max(0, final_earned - total_user_paid)

            net_user_paid = round(
                sum(p.amount - (p.refunded_amount or 0.0) for p in user_payments), 2
            )
            refund_amount = 0.0
            if remaining_due <= 0 and net_user_paid > final_earned:
                from app.modules.trips import payment_orchestrator

                intended_refund = round(net_user_paid - final_earned, 2)
                try:
                    refund_amount = payment_orchestrator.refund_trip_amount(
                        session,
                        trip_id,
                        intended_refund,
                        "Trip settlement refund (paid more than earned)",
                    )
                except Exception as refund_err:
                    # A refund failure must NOT be swallowed into a settlement
                    # marked paid — that silently loses the user's money. Roll the
                    # partial refund back (refund_payment never self-commits) and
                    # abort WITHOUT creating the settlement, so the daily settlement
                    # scheduler retries idempotently once the refund path recovers.
                    session.rollback()
                    session.exec(
                        select(Trip).where(Trip.id == trip_id).with_for_update()
                    ).first()
                    raced = session.exec(
                        select(TripSettlement).where(TripSettlement.trip_id == trip_id)
                    ).first()
                    if raced:
                        return (
                            False,
                            raced.id,
                            "Settlement already exists for this trip",
                        )
                    logger.error(
                        "Settlement refund failed for trip %s (intended ₹%.2f): %s",
                        trip_id,
                        intended_refund,
                        refund_err,
                    )
                    audit_emit(
                        "settlement.refund_failed",
                        trip_id=trip_id,
                        actor="system",
                        payload={
                            "intended_refund": intended_refund,
                            "error": str(refund_err),
                        },
                    )
                    return (
                        False,
                        None,
                        f"Settlement refund failed; will retry: {refund_err}",
                    )

            settlement = TripSettlement(
                trip_id=trip_id,
                user_id=trip.user_id,
                driver_id=trip.driver_id,
                settlement_status="generated",
                total_trips=total_trips,
                completed_trips=present_count,
                absent_trips=absent_count,
                skipped_trips=absent_count,
                total_earned=final_earned,
                total_paid_upfront=total_user_paid,
                remaining_due=remaining_due,
                refund_amount=refund_amount,
                user_payment_status="pending" if remaining_due > 0 else "paid",
                driver_payment_status="paid",
                settlement_date=today_ist(),
            )
            session.add(settlement)
            session.commit()
            session.refresh(settlement)

            return True, settlement.id, None

        except Exception as e:
            return False, None, f"Final settlement generation failed: {str(e)}"

    def get_bill_details(
        self, session: Session, bill_id: int
    ) -> Optional[Dict[str, Any]]:
        try:
            bill = session.get(TripBill, bill_id)
            if not bill:
                return None

            components_records = session.exec(
                select(PricingComponentBreakdown).where(
                    PricingComponentBreakdown.bill_id == bill_id
                )
            ).all()

            return {
                "id": bill.id,
                "trip_id": bill.trip_id,
                "bill_type": bill.bill_type,
                "bill_date": bill.bill_date,
                "total_amount": bill.total_amount,
                "amount_paid": bill.amount_paid,
                "amount_due": bill.amount_due,
                "discount_percentage": bill.discount_percentage,
                "is_paid": bill.is_paid,
                "paid_at": bill.paid_at,
                "components": bill.components,
                "generated_at": bill.generated_at,
            }

        except Exception as e:
            return None

    def get_settlement_details(
        self, session: Session, settlement_id: int
    ) -> Optional[Dict[str, Any]]:
        try:
            settlement = session.get(TripSettlement, settlement_id)
            if not settlement:
                return None

            return {
                "id": settlement.id,
                "trip_id": settlement.trip_id,
                "settlement_status": settlement.settlement_status,
                "total_trips": settlement.total_trips,
                "completed_trips": settlement.completed_trips,
                "absent_trips": settlement.absent_trips,
                "skipped_trips": settlement.skipped_trips,
                "total_earned": settlement.total_earned,
                "total_paid_upfront": settlement.total_paid_upfront,
                "remaining_due": settlement.remaining_due,
                "refund_amount": settlement.refund_amount,
                "user_payment_status": settlement.user_payment_status,
                "driver_payment_status": settlement.driver_payment_status,
                "settlement_date": settlement.settlement_date,
                "due_date": settlement.due_date,
                "paid_at": settlement.paid_at,
            }

        except Exception as e:
            return None
