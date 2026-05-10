"""
Billing and Settlement Service for Trip Management
"""

from datetime import date

from app.utils.time_utils import today_ist, now_ist
from typing import Optional, Tuple, Dict, Any
from sqlmodel import Session, select

from app.core.models import (
    Trip,
    Driver,
    TripAttendance,
    PaymentTransaction,
    TripBill,
    TripSettlement,
    PricingComponentBreakdown,
)


class BillingService:
    """
    Handles bill generation and final settlement
    """

    def calculate_daily_bill_components(
        self, session: Session, trip_id: int, trip_date: date
    ) -> Tuple[Dict[str, float], float, Optional[str]]:
        """
        Calculate billing components for a specific day.

        (Derives from trip.fare_breakdown instead of hardcoded assumptions)

        For multi-day trips:
          - trip.fare is the TOTAL for all days
          - trip.fare_breakdown contains the per-component split (e.g., Driver Allowance (N days), ...)
          - daily_total = trip.fare / num_days (from attendance count)

        Components:
        - Base fare (prorated daily)
        - Vehicle allowance (prorated daily)
        - Taxes (prorated daily)

        Returns:
            Tuple of (components_dict, total_amount, error_message)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return {}, 0.0, "Trip not found"

            # Get all attendance records for this trip to calculate per-day amount
            attendances = session.exec(
                select(TripAttendance).where(TripAttendance.trip_id == trip_id)
            ).all()

            num_days = len(attendances)
            if num_days == 0:
                return {}, 0.0, "No attendance records found for trip"

            # If trip.fare and fare_breakdown exist, derive daily components
            if trip.fare and trip.fare_breakdown:
                # Simple pro-rata: daily_total = total / num_days
                daily_total = trip.fare / num_days

                # fare_breakdown is the full dict produced by pricing_calculator
                # (keys: total, subtotal, tax, currency, components, meta). The
                # itemised component list lives at fare_breakdown["components"].
                # Older rows may have stored the bare list directly.
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

                # Fallback if component list was empty/unparseable: keep the
                # daily total visible as a single line so the bill is never blank.
                if not components:
                    components["Base Fare"] = daily_total

                return components, daily_total, None

            # Fallback if fare_breakdown is not populated (shouldn't happen with new engine)
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
        """
        Generate a daily bill for a trip day

        (auto-mark bill is_paid=True when amount_due == 0 at generation time,
        so advance_20 and full_payment upfront payments don't create unpaid bills.)

        Returns:
            Tuple of (success, bill_id, error_message)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, None, "Trip not found"

            # Check if bill already exists for this day
            existing_bill = session.exec(
                select(TripBill).where(
                    TripBill.trip_id == trip_id,
                    TripBill.bill_type == "daily_bill",
                    TripBill.bill_date == trip_date,
                )
            ).first()

            if existing_bill:
                return False, existing_bill.id, "Bill already exists for this day"

            # Calculate components
            components, total_amount, error = self.calculate_daily_bill_components(
                session, trip_id, trip_date
            )

            if error:
                return False, None, error

            # Get existing payment
            existing_payments = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "user",
                    PaymentTransaction.payment_status == "success",
                )
            ).all()

            amount_paid = sum(p.amount for p in existing_payments)
            amount_due = max(0, total_amount - amount_paid)

            is_paid = amount_due == 0

            # Build the JSON-friendly components list (uniform schema across the system)
            components_list = [
                {
                    "name": name,
                    "amount": amount,
                    "percentage": (amount / total_amount * 100)
                    if total_amount > 0
                    else 0.0,
                }
                for name, amount in components.items()
            ]

            # Create bill
            bill = TripBill(
                trip_id=trip_id,
                user_id=trip.user_id,
                driver_id=trip.driver_id,
                bill_type="daily_bill",
                bill_date=trip_date,
                total_amount=total_amount,
                amount_paid=amount_paid,
                amount_due=amount_due,
                is_paid=is_paid,
                paid_at=now_ist() if is_paid else None,  # Also set paid_at if auto-paid
                is_generated=True,
                components=components_list,
            )
            session.add(bill)
            session.flush()  # need bill.id for component FK linkage

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

    def generate_final_settlement(
        self, session: Session, trip_id: int
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Generate final settlement after all trip days complete

        Returns:
            Tuple of (success, settlement_id, error_message)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, None, "Trip not found"

            # Check if settlement already exists (idempotency)
            existing_settlement = session.exec(
                select(TripSettlement).where(TripSettlement.trip_id == trip_id)
            ).first()

            if existing_settlement:
                return (
                    False,
                    existing_settlement.id,
                    "Settlement already exists for this trip",
                )

            # Get all attendance records
            attendances = session.exec(
                select(TripAttendance).where(TripAttendance.trip_id == trip_id)
            ).all()

            # Count trip statuses
            present_count = len([a for a in attendances if a.status == "present"])
            absent_count = len([a for a in attendances if "skipped" in a.status])
            total_trips = len(attendances)

            # Calculate total earned
            daily_bills = session.exec(
                select(TripBill).where(
                    TripBill.trip_id == trip_id, TripBill.bill_type == "daily_bill"
                )
            ).all()

            total_earned = sum(bill.total_amount for bill in daily_bills)

            # Calculate payments
            user_payments = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "user",
                    PaymentTransaction.payment_status == "success",
                )
            ).all()

            driver_payments = session.exec(
                select(PaymentTransaction).where(
                    PaymentTransaction.trip_id == trip_id,
                    PaymentTransaction.payer_type == "driver",
                    PaymentTransaction.payment_status == "success",
                )
            ).all()

            total_user_paid = sum(p.amount for p in user_payments)
            total_driver_paid = sum(p.amount for p in driver_payments)

            # Apply payment method discount
            discount_percentage = 0.0
            if trip.payment_method == "full_payment":
                discount_percentage = 5.0

            discount_amount = total_earned * (discount_percentage / 100)
            final_earned = total_earned - discount_amount

            # Calculate remaining balance
            remaining_due = max(0, final_earned - total_user_paid)

            # Create settlement
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
        """
        Get detailed bill information

        Returns:
            Dict with bill details or None
        """
        try:
            bill = session.get(TripBill, bill_id)
            if not bill:
                return None

            # Get components
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
        """
        Get detailed settlement information

        Returns:
            Dict with settlement details or None
        """
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
