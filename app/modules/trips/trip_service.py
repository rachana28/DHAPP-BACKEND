"""
Trip Service for managing trip lifecycle and state machine
"""

from datetime import datetime, timedelta, date
from typing import Optional, Tuple, Dict, Any
from sqlmodel import Session, select
import re

from app.core.models import (
    Trip,
    TripAttendance,
    PaymentTransaction,
)


class TripService:
    """
    Manages trip state machine and lifecycle

    State Flow:
    searching → accepted_pending_payment → active_pending_otp → active → ongoing → completed → billed
    """

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
        # active_pending_otp → skipped/completed lets a skipped final shift wrap the trip up
        "active_pending_otp": [
            "active",
            "otp_expired",
            "cancelled",
            "skipped",
            "completed",
        ],
        "active": ["ongoing", "skipped", "cancelled_by_user", "cancelled_by_driver"],
        # ongoing → active_pending_otp lets multi-day trips re-arm OTP for the next shift day
        "ongoing": ["completed", "auto_completed", "paused", "active_pending_otp"],
        # paused → active_pending_otp re-arms the next shift's OTP after a
        # trip_day user clears the outstanding daily bill that held the trip.
        "paused": ["ongoing", "completed", "active_pending_otp"],
        "completed": ["billed", "active_pending_otp"],
        "auto_completed": ["billed", "active_pending_otp"],
        "billed": ["settled"],
        "skipped": ["billed"],
        "cancelled_by_user": ["refund_processing"],
        "cancelled_by_driver": ["refund_processing"],
        "refund_processing": ["settled"],
    }

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
    ) -> Tuple[bool, Optional[str]]:
        try:
            # Row-lock the trip to serialize against parallel transitions
            # (driver end-trip vs auto-end scheduler, etc.). Without this
            # state_version is incremented in lockstep but two transitions
            # can still both succeed and race on side effects.
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

            trip.status = new_state
            trip.state_version += 1
            session.add(trip)
            session.commit()

            return True, None

        except Exception as e:
            return False, f"State transition failed: {str(e)}"

    def has_pending_shifts(self, session: Session, trip_id: int) -> bool:
        """True iff at least one attendance row is still scheduled / paused_payment."""
        row = session.exec(
            select(TripAttendance).where(
                TripAttendance.trip_id == trip_id,
                TripAttendance.status.in_(["scheduled", "paused_payment"]),
            )
        ).first()
        return row is not None

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
    ) -> Tuple[bool, Optional[str]]:
        try:
            day_filter = self.parse_selected_days(selected_days)
            current_date = start_date
            created = 0
            start_hour = trip_start_dt.hour
            start_minute = trip_start_dt.minute

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

            session.commit()
            if created == 0:
                return (
                    False,
                    "No attendance days created (selected_days may not match range)",
                )
            return True, None

        except Exception as e:
            session.rollback()
            return False, f"Attendance record creation failed: {str(e)}"

    # Trip states from which a user-initiated cancel is structurally impossible.
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
        """User-initiated cancellation gate.

        Rules (real-time scenarios):
          * Driver-initiated cancellations are NOT supported here — drivers must
            reject offers via /accept-and-pay action="reject" instead.
          * Terminal / non-cancellable states are blocked.
          * status == "ongoing" or "active": a shift is physically running, the
            driver is engaged — block until it ends.
          * payment_method == "trip_day": cancel allowed in any non-terminal
            state PROVIDED there is no outstanding daily bill and the trip is
            not paused for payment. Completed shifts are already paid for, so
            no refund is owed and no driver is at a loss for those days.
          * payment_method in ("advance_20", "full_payment"): cancel allowed
            ONLY if no shift has ever been started (actual_start_time is None
            and no attendance is marked present). Once any shift is done, the
            trip must run to completion so the deferred / upfront balance is
            settled normally.

        Returns:
            (allowed, reason, has_completed_shift)
        """
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found", False

            # Driver flow is no longer supported.
            if driver_id is not None:
                return False, "Drivers cannot cancel trips. Reject the offer instead.", False

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

            # A shift is currently in progress — driver is engaged.
            if trip.status in ("ongoing", "active"):
                return (
                    False,
                    "Trip is currently in progress. Wait for the shift to end before cancelling.",
                    has_completed_shift,
                )

            if trip.payment_method == "trip_day":
                # Pause due to outstanding daily bill: user must clear the bill first.
                if trip.is_payment_blocked or trip.status == "paused":
                    return (
                        False,
                        "Trip is paused due to an unpaid bill. Pay the outstanding amount before cancelling.",
                        has_completed_shift,
                    )
                # Caller (router) does the unpaid-bill check via PaymentService
                # so the gate here stays free of payment lookups beyond the
                # trip's own flags.
                return True, None, has_completed_shift

            if trip.payment_method in ("advance_20", "full_payment"):
                if has_completed_shift:
                    return (
                        False,
                        "Cancellation not allowed after a shift has started for this payment method. "
                        "The trip will close automatically after the final shift.",
                        has_completed_shift,
                    )
                return True, None, has_completed_shift

            # Payment method not yet selected (early states like "searching").
            # No payment exists, so cancel is safe.
            return True, None, has_completed_shift

        except Exception as e:
            return False, f"Cancellation validation failed: {str(e)}", False

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

            # Block skipping a shift that has already physically started.
            # End-trip is the correct path once OTP has been verified.
            if attendance.user_otp_verified or attendance.driver_otp_verified:
                return (
                    False,
                    "Cannot skip a day that has already started. Use end-trip instead.",
                )

            status = f"skipped_by_{marked_by}"
            attendance.status = status
            attendance.skip_reason = skip_reason
            attendance.marked_by = marked_by
            session.add(attendance)
            session.commit()

            # If this was the last pending shift, push the trip to a terminal
            # state so it doesn't sit forever in active_pending_otp / paused.
            if not self.has_pending_shifts(session, trip_id):
                trip = session.get(Trip, trip_id)
                if trip and trip.status in (
                    "active_pending_otp",
                    "paused",
                    "ongoing",
                ):
                    # If no shift was ever started, the trip closes as `skipped`;
                    # otherwise it closes as `completed`.
                    has_any_present = session.exec(
                        select(TripAttendance).where(
                            TripAttendance.trip_id == trip_id,
                            TripAttendance.status == "present",
                        )
                    ).first()
                    target = "completed" if has_any_present else "skipped"
                    # Allow direct hop from paused/ongoing to skipped/completed.
                    self.transition_trip_state(
                        session, trip_id, target, validate=False
                    )

            return True, None

        except Exception as e:
            return False, f"Marking absent failed: {str(e)}"

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

            # Source of truth: attendance rows. Trip-level scheduled/actual fields
            # rotate to the *current* shift, so they don't represent the booking's
            # overall start — and they may be null before the first shift fires.
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
                # Attendance rows aren't created until the driver-payment callback
                # runs. Synthesize the planned schedule so summaries before that
                # point still show meaningful totals/timings.
                total_days = self._expected_total_days(
                    trip.start_date, trip.end_date, trip.selected_days
                )
                # No attendance rows yet => every planned day is pending.
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

            # Final fallback: never return null for scheduled_start when start_date exists.
            if not scheduled_start and trip.start_date:
                scheduled_start = self.get_trip_start_time(
                    trip.shift_details, trip.start_date
                ) or datetime.combine(trip.start_date, datetime.min.time())

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

            result = {
                "trip_id": trip.id,
                "status": trip.status,
                "payment_method": trip.payment_method,
                "start_date": trip.start_date,
                "end_date": trip.end_date,
                "total_days": total_days,
                "present_days": present_count,
                "absent_days": absent_count,
                "pending_days": pending_count,
                "total_user_paid": sum(p.amount for p in user_payments),
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
                "actual_start": actual_start,
                "actual_end": actual_end,
                "fare": trip.fare,
                "fare_breakdown": trip.fare_breakdown,
            }

            if is_driver:
                result["total_driver_paid"] = sum(p.amount for p in driver_payments)

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
