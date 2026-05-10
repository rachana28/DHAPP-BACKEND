"""
Trip Service for managing trip lifecycle and state machine
"""

from datetime import datetime, timedelta, date
from typing import Optional, Tuple, Dict, Any
from sqlmodel import Session, select

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
        "active_pending_otp": ["active", "otp_expired", "cancelled"],
        "active": ["ongoing", "skipped", "cancelled_by_user", "cancelled_by_driver"],
        # ongoing → active_pending_otp lets multi-day trips re-arm OTP for the next shift day
        "ongoing": ["completed", "auto_completed", "paused", "active_pending_otp"],
        "paused": ["ongoing", "completed"],
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
        self, session: Session, trip_id: int, new_state: str, validate: bool = True
    ) -> Tuple[bool, Optional[str]]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

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

    def get_trip_duration_hours(self, shift_details: Optional[str]) -> Optional[int]:
        if not shift_details:
            return None

        try:
            parts = shift_details.split()
            for i, part in enumerate(parts):
                if part.lower() == "hours":
                    return int(parts[i - 1])
        except (ValueError, IndexError):
            return None

        return None

    def get_trip_start_time(
        self, shift_details: Optional[str], trip_date: date
    ) -> Optional[datetime]:
        if not shift_details:
            return None

        try:
            start_idx = shift_details.find("(") + 1
            end_idx = shift_details.find(")")
            time_str = shift_details[start_idx:end_idx]  # "15:00"

            hour, minute = map(int, time_str.split(":"))
            return datetime.combine(
                trip_date, datetime.min.time().replace(hour=hour, minute=minute)
            )
        except (ValueError, AttributeError):
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

    def can_cancel_trip(
        self,
        session: Session,
        trip_id: int,
        user_id: Optional[str] = None,
        driver_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        try:
            trip = session.get(Trip, trip_id)
            if not trip:
                return False, "Trip not found"

            if user_id and str(trip.user_id) != user_id:
                return False, "Not authorized to cancel this trip"

            if driver_id and trip.driver_id != driver_id:
                return False, "Not authorized to cancel this trip"

            if (
                trip.payment_method == "advance_20"
                or trip.payment_method == "full_payment"
            ):
                payments = session.exec(
                    select(PaymentTransaction).where(
                        PaymentTransaction.trip_id == trip_id,
                        PaymentTransaction.payment_status == "success",
                    )
                ).all()

                if len(payments) > 0:
                    return (
                        False,
                        f"Cannot cancel trip with {trip.payment_method} payment method",
                    )

            if trip.actual_start_time is not None:
                return False, "Cannot cancel trip that has already started"

            return True, None

        except Exception as e:
            return False, f"Cancellation validation failed: {str(e)}"

    def mark_trip_day_present(
        self, session: Session, trip_id: int, trip_date: date
    ) -> Tuple[bool, Optional[str]]:
        try:
            attendance = session.exec(
                select(TripAttendance).where(
                    TripAttendance.trip_id == trip_id,
                    TripAttendance.trip_date == trip_date,
                )
            ).first()

            if not attendance:
                return False, f"Attendance record not found for {trip_date}"

            attendance.status = "present"
            attendance.user_otp_verified = True
            attendance.driver_otp_verified = True
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
                select(TripAttendance).where(
                    TripAttendance.trip_id == trip_id,
                    TripAttendance.trip_date == trip_date,
                )
            ).first()

            if not attendance:
                return False, f"Attendance record not found for {trip_date}"

            status = f"skipped_by_{marked_by}"
            attendance.status = status
            attendance.skip_reason = skip_reason
            attendance.marked_by = marked_by
            session.add(attendance)
            session.commit()

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
                select(TripAttendance).where(TripAttendance.trip_id == trip_id)
            ).all()

            present_count = len([a for a in attendances if a.status == "present"])
            absent_count = len([a for a in attendances if "skipped" in a.status])

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
                "total_days": len(attendances),
                "present_days": present_count,
                "absent_days": absent_count,
                "total_user_paid": sum(p.amount for p in user_payments),
                "scheduled_start": trip.scheduled_start_time,
                "scheduled_end": trip.scheduled_end_time,
                "actual_start": trip.actual_start_time,
                "actual_end": trip.actual_end_time,
                "fare": trip.fare,
                "fare_breakdown": trip.fare_breakdown,
            }

            if is_driver:
                result["total_driver_paid"] = sum(p.amount for p in driver_payments)

            return result

        except Exception as e:
            return None
