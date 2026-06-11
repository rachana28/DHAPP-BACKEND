"""
Trip lifecycle state machine + per-shift bookkeeping.

Holds the allowed status transitions, the driver-busy set used by allocation,
the rolling driver-skip limit, and the attendance helpers that the router and
schedulers call into. All status changes should flow through
:meth:`TripService.transition_trip_state` so the optimistic ``state_version``
bumps in lockstep and an audit event is emitted.
"""

from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple, Dict, Any
from sqlmodel import Session, select, func
import re

from app.core.models import (
    Trip,
    TripAttendance,
    Payment,
    TripBill,
    TripSettlement,
    Driver,
    OTPRegistry,
    User,
)
from app.modules.trips.billing_service import payment_method_discount_pct
from app.services.audit_log import emit_event as audit_emit
from app.utils.time_utils import now_ist, today_ist


class TripService:
    VALID_STATES = {
        "searching": ["accepted_pending_payment", "no_drivers_found", "cancelled"],
        "accepted_pending_payment": [
            "payment_in_progress",
            "active_pending_otp",
            "rejected",
            "searching",
        ],
        "payment_in_progress": ["active_pending_otp", "payment_failed"],
        "payment_failed": ["searching", "cancelled"],
        "rejected": ["searching", "cancelled"],
        "active_pending_otp": [
            "active",
            "otp_expired",
            "cancelled",
            "skipped",
            "completed",
            "paused",
            "cancelled_by_driver",
        ],
        "active": ["ongoing", "skipped", "cancelled_by_user", "cancelled_by_driver"],
        "ongoing": [
            "completed",
            "auto_completed",
            "paused",
            "active_pending_otp",
            "cancelled_by_driver",
        ],
        "paused": [
            "ongoing",
            "completed",
            "active_pending_otp",
            "billed",
            "cancelled_by_driver",
        ],
        "completed": ["billed", "active_pending_otp"],
        "auto_completed": ["billed", "active_pending_otp"],
        "billed": ["settled"],
        "skipped": ["billed"],
        "cancelled_by_user": ["refund_processing", "cancellation_pending_payment"],
        "cancelled_by_driver": ["refund_processing"],
        "refund_processing": ["settled"],
        "cancellation_pending_payment": ["settled"],
    }

    DRIVER_BUSY_STATES = (
        "accepted_pending_payment",
        "payment_in_progress",
        "active_pending_otp",
        "active",
        "ongoing",
        "paused",
    )

    def __init__(self):
        pass

    def validate_state_transition(
        self, current_state: str, new_state: str
    ) -> Tuple[bool, Optional[str]]:
        if current_state not in self.VALID_STATES:
            return False, f"Unknown current state: {current_state}"

        allowed_states = self.VALID_STATES.get(current_state, [])

        if new_state not in allowed_states:
            return (
                False,
                f"Invalid transition from {current_state} to {new_state}. Allowed: {allowed_states}",
            )

        return True, None

    def transition_trip_state(
        self,
        session: Session,
        trip_id: int,
        new_state: str,
        validate: bool = True,
        expected_version: Optional[int] = None,
        commit: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        try:
            trip = session.exec(
                select(Trip).where(Trip.id == trip_id).with_for_update()
            ).first()
            if not trip:
                return False, "Trip not found"

            if expected_version is not None and trip.state_version != expected_version:
                return False, "Trip state changed concurrently; please retry"

            current_state = trip.status

            if validate:
                is_valid, error = self.validate_state_transition(
                    current_state, new_state
                )
                if not is_valid:
                    return False, error

            previous_state = current_state
            trip.status = new_state
            trip.state_version += 1
            session.add(trip)
            if commit:
                session.commit()
            else:
                session.flush()

            audit_emit(
                "trip.state_transition",
                trip_id=trip_id,
                actor="system",
                payload={
                    "from": previous_state,
                    "to": new_state,
                    "state_version": trip.state_version,
                    "validated": validate,
                },
            )
            return True, None

        except Exception as e:
            return False, f"State transition failed: {str(e)}"

    def has_pending_shifts(self, session: Session, trip_id: int) -> bool:
        row = session.exec(
            select(TripAttendance).where(
                TripAttendance.trip_id == trip_id,
                TripAttendance.status.in_(["scheduled", "paused_payment"]),
            )
        ).first()
        return row is not None

    def compute_outstanding_portion(
        self, session: Session, trip: Trip
    ) -> Dict[str, Any]:
        fare = trip.fare or 0.0

        attendances = session.exec(
            select(TripAttendance).where(TripAttendance.trip_id == trip.id)
        ).all()
        total_att = len(attendances)
        per_day = (fare / total_att) if total_att else fare

        remaining_count = len(
            [a for a in attendances if a.status in ("scheduled", "paused_payment")]
        )

        daily_bills = session.exec(
            select(TripBill)
            .where(
                TripBill.trip_id == trip.id,
                TripBill.bill_type == "daily_bill",
            )
            .order_by(TripBill.bill_date)
        ).all()
        unsettled_bills = [b for b in daily_bills if (b.amount_due or 0.0) > 0]
        settled_paid = sum(
            (b.amount_paid or 0.0) for b in daily_bills if (b.amount_due or 0.0) <= 0
        )

        billed_dates = {b.bill_date for b in daily_bills}
        completed_unbilled = [
            a
            for a in attendances
            if a.status == "present" and a.trip_date not in billed_dates
        ]
        discount_pct = payment_method_discount_pct(
            trip.hiring_type, trip.payment_method
        )
        per_day_net = per_day * (1 - discount_pct / 100.0)
        consumed_advfull = round(per_day_net * len(completed_unbilled), 2)

        if total_att == 0:
            outstanding_gross = round(fare, 2)
        else:
            outstanding_gross = round(
                per_day * (remaining_count + len(unsettled_bills)), 2
            )

        user_paid = sum(
            p.amount
            for p in session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip.id,
                    Payment.payer_type == "user",
                    Payment.status.in_(["succeeded", "partially_refunded"]),
                )
            ).all()
        )
        payments_applied = max(
            0.0, round(user_paid - settled_paid - consumed_advfull, 2)
        )

        return {
            "per_day": per_day,
            "remaining_count": remaining_count,
            "unsettled_bills": unsettled_bills,
            "outstanding_gross": outstanding_gross,
            "payments_applied": payments_applied,
            "total_attendance": total_att,
        }

    def get_trip_duration_hours(self, shift_details: Optional[str]) -> Optional[int]:
        if not shift_details:
            return None

        try:
            match = re.search(r"(\d+)\s*Hour", shift_details, re.IGNORECASE)
            if match:
                return int(match.group(1))
        except Exception:
            pass

        return None

    def get_trip_start_time(
        self, shift_details: Optional[str], trip_date: date
    ) -> Optional[datetime]:
        if not shift_details:
            return None

        try:
            match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?", shift_details)
            if match:
                hour = int(match.group(1))
                minute = int(match.group(2))
                meridiem = match.group(3)

                if meridiem:
                    if meridiem.lower() == "pm" and hour < 12:
                        hour += 12
                    elif meridiem.lower() == "am" and hour == 12:
                        hour = 0

                return datetime.combine(
                    trip_date, datetime.min.time().replace(hour=hour, minute=minute)
                )
        except Exception:
            pass

        return None

    _DAY_TOKEN_MAP = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tues": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }

    def parse_selected_days(self, selected_days: Optional[str]) -> Optional[set]:
        if not selected_days:
            return None
        result = set()
        for token in selected_days.replace("/", ",").replace("|", ",").split(","):
            t = token.strip().lower()
            if t in self._DAY_TOKEN_MAP:
                result.add(self._DAY_TOKEN_MAP[t])
        return result or None

    def create_trip_attendance_records(
        self,
        session: Session,
        trip_id: int,
        start_date: date,
        end_date: date,
        trip_start_dt: datetime,
        trip_duration_hours: int,
        selected_days: Optional[str] = None,
        single_shift: bool = False,
        commit: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        try:
            start_hour = trip_start_dt.hour
            start_minute = trip_start_dt.minute

            if single_shift:
                scheduled_start = datetime.combine(
                    start_date,
                    datetime.min.time().replace(hour=start_hour, minute=start_minute),
                )
                scheduled_end = datetime.combine(
                    end_date,
                    datetime.min.time().replace(hour=start_hour, minute=start_minute),
                ) + timedelta(hours=trip_duration_hours)
                session.add(
                    TripAttendance(
                        trip_id=trip_id,
                        trip_date=start_date,
                        status="scheduled",
                        marked_by="system",
                        user_otp_verified=False,
                        driver_otp_verified=False,
                        scheduled_start=scheduled_start,
                        scheduled_end=scheduled_end,
                    )
                )
                if commit:
                    session.commit()
                else:
                    session.flush()
                return True, None

            day_filter = self.parse_selected_days(selected_days)
            current_date = start_date
            created = 0

            while current_date <= end_date:
                if day_filter is None or current_date.weekday() in day_filter:
                    scheduled_start = datetime.combine(
                        current_date,
                        datetime.min.time().replace(
                            hour=start_hour, minute=start_minute
                        ),
                    )
                    scheduled_end = scheduled_start + timedelta(
                        hours=trip_duration_hours
                    )

                    attendance = TripAttendance(
                        trip_id=trip_id,
                        trip_date=current_date,
                        status="scheduled",
                        marked_by="system",
                        user_otp_verified=False,
                        driver_otp_verified=False,
                        scheduled_start=scheduled_start,
                        scheduled_end=scheduled_end,
                    )
                    session.add(attendance)
                    created += 1

                current_date = current_date + timedelta(days=1)

            if created == 0:
                return (
                    False,
                    "No attendance days created (selected_days may not match range)",
                )
            if commit:
                session.commit()
            else:
                session.flush()
            return True, None

        except Exception as e:
            session.rollback()
            return False, f"Attendance record creation failed: {str(e)}"

    _NON_CANCELLABLE_STATES = (
        "completed",
        "auto_completed",
        "cancelled",
        "cancelled_by_user",
        "cancelled_by_driver",
        "billed",
        "settled",
        "skipped",
        "refund_processing",
        "no_drivers_found",
        "rejected",
        "payment_failed",
    )

    def can_cancel_trip(
        self,
        session: Session,
        trip_id: int,
        user_id: Optional[str] = None,
        driver_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], bool]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found", False

            if driver_id is not None:
                return (
                    False,
                    "Drivers cannot cancel trips. Reject the offer instead.",
                    False,
                )

            if user_id and str(trip.user_id) != user_id:
                return False, "Not authorized to cancel this trip", False

            has_completed_shift = bool(trip.actual_start_time) or (
                session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == trip_id,
                        TripAttendance.status == "present",
                    )
                ).first()
                is not None
            )

            if trip.status in self._NON_CANCELLABLE_STATES:
                return (
                    False,
                    "Trip cannot be cancelled in its current state.",
                    has_completed_shift,
                )

            if trip.status in ("ongoing", "active"):
                return (
                    False,
                    "Trip is currently in progress. Wait for the shift to end before cancelling.",
                    has_completed_shift,
                )

            if trip.payment_method == "trip_day":
                if trip.is_payment_blocked or trip.status == "paused":
                    return (
                        False,
                        "Trip is paused due to an unpaid bill. Pay the outstanding amount before cancelling.",
                        has_completed_shift,
                    )
                return True, None, has_completed_shift

            if trip.payment_method in ("advance_20", "full_payment"):
                if trip.is_payment_blocked or trip.status == "paused":
                    return (
                        False,
                        "Trip is paused awaiting your upfront payment. Pay the "
                        "upfront amount, or switch back to trip-day billing, "
                        "before cancelling.",
                        has_completed_shift,
                    )
                return True, None, has_completed_shift

            return True, None, has_completed_shift

        except Exception as e:
            return False, f"Cancellation validation failed: {str(e)}", False

    def get_selectable_payment_methods(self, session: Session, trip: Trip) -> list:
        if (trip.hiring_type or "").strip().lower() == "outstation":
            return []

        attendance_count = len(
            session.exec(
                select(TripAttendance.id).where(TripAttendance.trip_id == trip.id)
            ).all()
        )
        if attendance_count > 0 and not self.has_pending_shifts(session, trip.id):
            return []

        current = trip.payment_method
        upfront_pending = bool(trip.is_payment_blocked) and current in (
            "advance_20",
            "full_payment",
        )
        if current is None or current == "trip_day":
            return ["advance_20", "full_payment"]
        if current == "advance_20":
            return ["trip_day", "full_payment"] if upfront_pending else ["full_payment"]
        if current == "full_payment":
            return ["trip_day", "advance_20"] if upfront_pending else []
        return []

    def mark_trip_day_present(
        self,
        session: Session,
        trip_id: int,
        trip_date: date,
        actual_end: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        try:
            attendance = session.exec(
                select(TripAttendance)
                .where(
                    TripAttendance.trip_id == trip_id,
                    TripAttendance.trip_date == trip_date,
                )
                .with_for_update()
            ).first()

            if not attendance:
                return False, f"Attendance record not found for {trip_date}"

            attendance.status = "present"
            attendance.user_otp_verified = True
            attendance.driver_otp_verified = True
            if actual_end is not None and attendance.actual_end is None:
                attendance.actual_end = actual_end
            session.add(attendance)
            session.commit()

            return True, None

        except Exception as e:
            return False, f"Marking present failed: {str(e)}"

    DRIVER_SKIP_LIMIT_PER_TRIP_WINDOW = 3
    DRIVER_SKIP_WINDOW_DAYS = 30

    def count_driver_skips_for_trip_in_window(
        self, session: Session, trip_id: int, ref_date: date
    ) -> int:
        window_start = ref_date - timedelta(days=self.DRIVER_SKIP_WINDOW_DAYS - 1)
        rows = session.exec(
            select(TripAttendance.id).where(
                TripAttendance.trip_id == trip_id,
                TripAttendance.status == "skipped_by_driver",
                TripAttendance.trip_date >= window_start,
                TripAttendance.trip_date <= ref_date,
            )
        ).all()
        return len(rows)

    def driver_skips_remaining(
        self, session: Session, trip_id: int, ref_date: Optional[date] = None
    ) -> int:
        ref = ref_date or today_ist()
        used = self.count_driver_skips_for_trip_in_window(session, trip_id, ref)
        return max(0, self.DRIVER_SKIP_LIMIT_PER_TRIP_WINDOW - used)

    def mark_trip_day_absent(
        self,
        session: Session,
        trip_id: int,
        trip_date: date,
        skip_reason: Optional[str] = None,
        marked_by: str = "user",
    ) -> Tuple[bool, Optional[str]]:
        try:
            attendance = session.exec(
                select(TripAttendance)
                .where(
                    TripAttendance.trip_id == trip_id,
                    TripAttendance.trip_date == trip_date,
                )
                .with_for_update()
            ).first()

            if not attendance:
                return False, f"Attendance record not found for {trip_date}"

            if attendance.status not in ("scheduled", "paused_payment"):
                return (
                    False,
                    f"Cannot skip day with status '{attendance.status}'.",
                )

            if attendance.user_otp_verified or attendance.driver_otp_verified:
                return (
                    False,
                    "Cannot skip a day that has already started. Use end-trip instead.",
                )

            if marked_by == "driver":
                used = self.count_driver_skips_for_trip_in_window(
                    session, trip_id, trip_date
                )
                if used >= self.DRIVER_SKIP_LIMIT_PER_TRIP_WINDOW:
                    return (
                        False,
                        f"Skip limit reached — a driver can skip at most "
                        f"{self.DRIVER_SKIP_LIMIT_PER_TRIP_WINDOW} shifts in any "
                        f"{self.DRIVER_SKIP_WINDOW_DAYS}-day window on a trip booking.",
                    )

            status = f"skipped_by_{marked_by}"
            attendance.status = status
            attendance.skip_reason = skip_reason
            attendance.marked_by = marked_by
            session.add(attendance)
            session.commit()

            if not self.has_pending_shifts(session, trip_id):
                trip = session.get(Trip, trip_id)
                if trip and trip.status in (
                    "active_pending_otp",
                    "paused",
                    "ongoing",
                ):
                    has_any_present = session.exec(
                        select(TripAttendance).where(
                            TripAttendance.trip_id == trip_id,
                            TripAttendance.status == "present",
                        )
                    ).first()
                    target = "completed" if has_any_present else "skipped"
                    self.transition_trip_state(session, trip_id, target, validate=False)

            return True, None

        except Exception as e:
            return False, f"Marking absent failed: {str(e)}"

    def build_driver_actions(
        self, session: Session, trip: Trip, attendances: List[TripAttendance]
    ) -> Dict[str, Any]:
        now = now_ist()
        today = today_ist()

        next_shift_att = next(
            (
                a
                for a in attendances
                if a.status in ("scheduled", "paused_payment")
                and not a.user_otp_verified
            ),
            None,
        )

        next_shift = None
        if next_shift_att is not None:
            next_shift = {
                "trip_date": next_shift_att.trip_date,
                "scheduled_start": next_shift_att.scheduled_start,
                "scheduled_end": next_shift_att.scheduled_end,
                "is_today": next_shift_att.trip_date == today,
            }

        can_verify_otp = bool(
            trip.status == "active_pending_otp"
            and not trip.is_payment_blocked
            and next_shift_att is not None
            and next_shift_att.scheduled_start - timedelta(minutes=30)
            <= now
            <= next_shift_att.scheduled_start + timedelta(hours=12)
        )

        otp_pending = False
        if next_shift_att is not None:
            otp_pending = (
                session.exec(
                    select(OTPRegistry).where(
                        OTPRegistry.attendance_id == next_shift_att.id,
                        OTPRegistry.verified_at.is_(None),
                        OTPRegistry.otp_expiry_at > now,
                    )
                ).first()
                is not None
            )

        skips_remaining = self.driver_skips_remaining(session, trip.id)
        can_skip = bool(
            next_shift_att is not None
            and next_shift_att.trip_date == today
            and not next_shift_att.driver_otp_verified
            and trip.status not in ("ongoing", "paused")
            and not trip.is_payment_blocked
            and (trip.hiring_type or "").strip().lower() != "outstation"
            and skips_remaining > 0
        )

        present_any = any(a.status == "present" for a in attendances)
        can_withdraw = bool(
            not present_any
            and trip.status
            in ("accepted_pending_payment", "active_pending_otp", "paused")
        )
        can_abandon = bool(
            present_any
            and trip.status in ("active_pending_otp", "active", "ongoing", "paused")
        )

        open_bills = session.exec(
            select(TripBill)
            .where(
                TripBill.trip_id == trip.id,
                TripBill.bill_type.in_(
                    ("daily_bill", "advance_recovery", "schedule_diff")
                ),
                TripBill.amount_due > 0,
            )
            .order_by(TripBill.bill_date)
        ).all()
        bill_to_collect = round(sum(b.amount_due or 0.0 for b in open_bills), 2)

        return {
            "next_shift": next_shift,
            "can_verify_otp": can_verify_otp,
            "otp_pending": otp_pending,
            "can_skip": can_skip,
            "skips_remaining": skips_remaining,
            "can_end": trip.status == "ongoing",
            "can_withdraw": can_withdraw,
            "can_abandon": can_abandon,
            "bill_to_collect": bill_to_collect,
            "open_bills": [
                {
                    "bill_id": b.id,
                    "bill_type": b.bill_type,
                    "bill_date": b.bill_date,
                    "amount_due": b.amount_due,
                }
                for b in open_bills
            ],
        }

    def get_trip_summary(
        self, session: Session, trip_id: int, is_driver: bool
    ) -> Optional[Dict[str, Any]]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return None

            attendances = session.exec(
                select(TripAttendance)
                .where(TripAttendance.trip_id == trip_id)
                .order_by(TripAttendance.trip_date)
            ).all()

            present_count = len([a for a in attendances if a.status == "present"])
            absent_count = len([a for a in attendances if "skipped" in a.status])
            pending_count = len(
                [a for a in attendances if a.status in ("scheduled", "paused_payment")]
            )

            if attendances:
                total_days = len(attendances)
                first_att = attendances[0]
                last_att = attendances[-1]

                next_pending = next(
                    (
                        a
                        for a in attendances
                        if a.status in ("scheduled", "paused_payment")
                    ),
                    None,
                )
                if next_pending is not None:
                    scheduled_start = next_pending.scheduled_start
                    scheduled_end = next_pending.scheduled_end
                else:
                    scheduled_start = first_att.scheduled_start
                    scheduled_end = last_att.scheduled_end

                actual_starts = [a.actual_start for a in attendances if a.actual_start]
                actual_ends = [a.actual_end for a in attendances if a.actual_end]
                actual_start = min(actual_starts) if actual_starts else None
                actual_end = max(actual_ends) if actual_ends else None
            else:
                total_days = self._expected_total_days(
                    trip.start_date, trip.end_date, trip.selected_days
                )
                pending_count = total_days
                scheduled_start = trip.scheduled_start_time or self.get_trip_start_time(
                    trip.shift_details, trip.start_date
                )
                duration_hours = (
                    self.get_trip_duration_hours(trip.shift_details)
                    or trip.trip_duration_hours
                )
                if trip.scheduled_end_time:
                    scheduled_end = trip.scheduled_end_time
                elif scheduled_start and duration_hours and trip.end_date:
                    scheduled_end = datetime.combine(
                        trip.end_date,
                        scheduled_start.time(),
                    ) + timedelta(hours=duration_hours)
                else:
                    scheduled_end = trip.scheduled_end_time
                actual_start = trip.actual_start_time
                actual_end = trip.actual_end_time

            if not scheduled_start and trip.start_date:
                scheduled_start = self.get_trip_start_time(
                    trip.shift_details, trip.start_date
                ) or datetime.combine(trip.start_date, datetime.min.time())

            _paid_states = ["succeeded", "partially_refunded", "refunded"]
            user_payments = session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_type == "user",
                    Payment.status.in_(_paid_states),
                )
            ).all()

            driver_payments = session.exec(
                select(Payment).where(
                    Payment.service_type == "trip",
                    Payment.service_id == trip_id,
                    Payment.payer_type == "driver",
                    Payment.status.in_(_paid_states),
                )
            ).all()

            user_paid = sum(p.amount for p in user_payments)

            daily_bills = session.exec(
                select(TripBill).where(
                    TripBill.trip_id == trip_id,
                    TripBill.bill_type == "daily_bill",
                )
            ).all()
            settlement = session.exec(
                select(TripSettlement).where(TripSettlement.trip_id == trip_id)
            ).first()

            upfront_amount_due = 0.0
            if trip.is_payment_blocked and trip.payment_method in (
                "advance_20",
                "full_payment",
            ):
                portion = self.compute_outstanding_portion(session, trip)
                rate = 0.95 if trip.payment_method == "full_payment" else 0.20
                upfront_amount_due = max(
                    0.0,
                    round(
                        rate * portion["outstanding_gross"]
                        - portion["payments_applied"],
                        2,
                    ),
                )

            if settlement is not None:
                amount_due = round(settlement.remaining_due or 0.0, 2)
            else:
                amount_due = round(sum((b.amount_due or 0.0) for b in daily_bills), 2)

            payment_notes: List[Dict[str, Any]] = []
            for b in daily_bills:
                if b.payment_note:
                    payment_notes.append(
                        {
                            "source": "daily_bill",
                            "bill_id": b.id,
                            "bill_date": b.bill_date,
                            "note": b.payment_note,
                            "paid_at": b.paid_at,
                        }
                    )
            if settlement is not None and settlement.payment_note:
                payment_notes.append(
                    {
                        "source": "settlement",
                        "settlement_id": settlement.id,
                        "note": settlement.payment_note,
                        "paid_at": settlement.paid_at,
                    }
                )

            result = {
                "trip_id": trip.reference_id,
                "status": trip.status,
                "hiring_type": trip.hiring_type,
                "payment_method": trip.payment_method,
                "start_date": trip.start_date,
                "end_date": trip.end_date,
                "total_days": total_days,
                "present_days": present_count,
                "absent_days": absent_count,
                "pending_days": pending_count,
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
                "actual_start": actual_start,
                "actual_end": actual_end,
                "fare": trip.fare,
                "fare_breakdown": trip.fare_breakdown,
            }

            if (trip.hiring_type or "").strip().lower() == "outstation":
                result["start_location"] = trip.start_location
                result["end_location"] = trip.end_location
                result["start_lat"] = trip.start_lat
                result["start_lng"] = trip.start_lng
                result["end_lat"] = trip.end_lat
                result["end_lng"] = trip.end_lng

            if is_driver:
                result["total_driver_paid"] = sum(p.amount for p in driver_payments)
                rider = session.get(User, trip.user_id)
                result["user"] = {
                    "full_name": rider.full_name if rider else None,
                    "avatar_url": rider.avatar_url if rider else None,
                }
                result["actions"] = self.build_driver_actions(
                    session, trip, attendances
                )
                return result

            result["total_user_paid"] = user_paid
            result["amount_due"] = amount_due
            result["payment_notes"] = payment_notes

            if trip.payment_method in ("advance_20", "full_payment"):
                result["upfront_amount_due"] = upfront_amount_due

            if (trip.status or "").startswith("cancel"):
                result["total_amount_refunded"] = round(
                    sum(p.refunded_amount or 0.0 for p in user_payments), 2
                )

            if (trip.hiring_type or "").strip().lower() == "outstation":
                result["extra_amount_paid"] = round(
                    (settlement.extra_amount_paid or 0.0) if settlement else 0.0,
                    2,
                )

            driver_detail: Dict[str, Any] = {}
            driver_paid_acceptance = trip.driver_payment_status == "paid" or any(
                p.purpose == "driver_acceptance" for p in driver_payments
            )
            if trip.driver_id is not None and driver_paid_acceptance:
                driver = session.get(Driver, trip.driver_id)
                if driver:
                    driver_total_trips = session.exec(
                        select(func.count(Trip.id)).where(Trip.driver_id == driver.id)
                    ).one()
                    driver_detail = {
                        "id": driver.reference_id,
                        "name": driver.name,
                        "rating": driver.rating,
                        "profile_picture_url": driver.profile_picture_url,
                        "vehicle_type": driver.vehicle_type,
                        "years_of_experience": driver.years_of_experience,
                        "total_trips": driver_total_trips,
                    }
            result["driver"] = driver_detail

            result["selectablePaymentMethod"] = self.get_selectable_payment_methods(
                session, trip
            )

            return result

        except Exception:
            return None

    def _expected_total_days(
        self,
        start_date: Optional[date],
        end_date: Optional[date],
        selected_days: Optional[str],
    ) -> int:
        if not start_date:
            return 0
        end = end_date or start_date
        if end < start_date:
            return 0
        day_filter = self.parse_selected_days(selected_days)
        if day_filter is None:
            return (end - start_date).days + 1
        count = 0
        current = start_date
        while current <= end:
            if current.weekday() in day_filter:
                count += 1
            current = current + timedelta(days=1)
        return count
