"""
Scheduled Jobs for Trip Management
- OTP generation
- Trip auto-end
- Driver payment auto-reject
- Daily settlement
"""

from datetime import timedelta
import logging

from sqlmodel import Session, select

from app.core.database import engine, get_redis
from app.core.models import (
    Trip,
    OTPRegistry,
    PaymentTransaction,
    TripAttendance,
    TripBill,
    TripSettlement,
    User,
    Driver,
)
from app.modules.trips.otp_service import OTPService
from app.modules.trips.trip_service import TripService
from app.modules.trips.billing_service import BillingService
from app.modules.trips.payment_service import PaymentService
from app.utils.time_utils import now_ist, today_ist
from app.utils.notifications import send_push_notification

logger = logging.getLogger(__name__)


async def generate_otp_for_trip_scheduler():
    """
    Background job: Generate the single trip-day OTP ~15 min before each shift.
    The user-app is expected to surface the OTP (via push or by polling /request-otp).
    The driver enters the OTP that the user shares verbally.
    """
    try:
        with Session(engine) as session:
            redis_client = get_redis()
            otp_service = OTPService(redis_client)

            now = now_ist()
            window_end = now + timedelta(minutes=20)
            today = today_ist()

            attendances = session.exec(
                select(TripAttendance).where(
                    TripAttendance.trip_date == today,
                    TripAttendance.scheduled_start >= now,
                    TripAttendance.scheduled_start <= window_end,
                    TripAttendance.status.notin_(
                        ["skipped_by_user", "skipped_by_driver", "skipped_by_system"]
                    ),
                )
            ).all()

            payment_service = PaymentService(redis_client)

            for att in attendances:
                trip = session.get(Trip, att.trip_id)
                if not trip or trip.status not in (
                    "active_pending_otp",
                    "ongoing",
                    "active",
                ):
                    continue

                # Pause check (trip_day method): if any prior daily bill is unpaid, skip
                # generating today's OTP, mark attendance as paused_payment, and remind the user.
                if (
                    trip.payment_method == "trip_day"
                    and payment_service.trip_has_unpaid_bills(session, trip.id)
                ):
                    if not trip.is_payment_blocked:
                        trip.is_payment_blocked = True
                        session.add(trip)
                    if att.status not in ("paused_payment", "skipped_by_system"):
                        att.status = "paused_payment"
                        att.skip_reason = "Outstanding daily bill"
                        att.marked_by = "system"
                        session.add(att)
                    session.commit()
                    try:
                        send_push_notification(
                            session=session,
                            user_ids=[trip.user_id],
                            title="Today's trip paused",
                            body="Settle your unpaid bills to resume your trips.",
                            data={"type": "trip_paused_payment", "trip_id": trip.id},
                        )
                    except Exception:
                        pass
                    continue

                # Skip if OTP for this day already exists
                existing = session.exec(
                    select(OTPRegistry).where(
                        OTPRegistry.trip_id == trip.id,
                        OTPRegistry.trip_date == today,
                    )
                ).first()
                if existing:
                    continue

                otp, err = otp_service.generate_otp(
                    session, trip.id, att.scheduled_start, trip_date=today
                )
                if err:
                    logger.error(
                        f"Failed to generate OTP for trip {trip.id} on {today}: {err}"
                    )
                else:
                    logger.info(f"OTP generated for trip {trip.id} on {today}")
                    # Push the OTP to the USER (driver receives it verbally)
                    # P3 fix: Don't expose OTP in notification body/data (lock-screen privacy)
                    try:
                        send_push_notification(
                            session=session,
                            user_ids=[trip.user_id],
                            title="Trip OTP Ready",
                            body=f"Your trip OTP is ready. Open the app to view it and share with your driver to start trip #{trip.id}.",
                            data={
                                "type": "trip_otp",
                                "trip_id": trip.id,
                            },  # Removed "otp" field
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"OTP generation scheduler failed: {str(e)}")


async def expire_otp_for_trip_scheduler():
    """
    Background job: For each (trip, day) where the OTP window closed without verification,
    mark that day's attendance as missed.
    """
    try:
        with Session(engine) as session:
            redis_client = get_redis()
            otp_service = OTPService(redis_client)

            now = now_ist()

            expired_rows = session.exec(
                select(OTPRegistry).where(
                    OTPRegistry.otp_expiry_at <= now,
                    OTPRegistry.verified_at.is_(None),
                )
            ).all()

            for row in expired_rows:
                trip = session.get(Trip, row.trip_id)
                if not trip:
                    continue

                attendance = session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == trip.id,
                        TripAttendance.trip_date == row.trip_date,
                    )
                ).first()
                if attendance and attendance.status not in (
                    "present",
                    "skipped_by_user",
                    "skipped_by_driver",
                    "skipped_by_system",
                ):
                    attendance.status = "skipped_by_system"
                    attendance.skip_reason = "OTP expired"
                    attendance.marked_by = "system"
                    session.add(attendance)
                    session.commit()

                otp_service.invalidate_otp(trip.id, row.trip_date)
                logger.info(
                    f"Trip {trip.id} OTP expired for {row.trip_date}; attendance marked missed"
                )
    except Exception as e:
        logger.error(f"OTP expiry scheduler failed: {str(e)}")


async def auto_end_trip_scheduler():
    """
    Background job: Automatically end trip after scheduled duration

    Called by APScheduler at trip_scheduled_end_time
    """
    try:
        with Session(engine) as session:
            trip_service = TripService()
            billing_service = BillingService()

            now = now_ist()

            # Find trips that should auto-end
            trips = session.exec(
                select(Trip).where(
                    Trip.status == "ongoing",
                    Trip.scheduled_end_time <= now,
                    Trip.actual_end_time.is_(None),
                )
            ).all()

            today = today_ist()
            for trip in trips:
                # If more shift days remain, re-arm OTP for the next day instead of terminating
                has_future_shifts = trip.end_date is not None and today < trip.end_date
                next_state = (
                    "active_pending_otp" if has_future_shifts else "auto_completed"
                )
                success, error = trip_service.transition_trip_state(
                    session, trip.id, next_state, validate=True
                )

                if success:
                    if has_future_shifts:
                        # Find the next TripAttendance after today
                        next_attendance = session.exec(
                            select(TripAttendance)
                            .where(
                                TripAttendance.trip_id == trip.id,
                                TripAttendance.trip_date > today,
                            )
                            .order_by(TripAttendance.trip_date)
                        ).first()

                        if next_attendance:
                            trip.scheduled_start_time = next_attendance.scheduled_start
                            trip.scheduled_end_time = next_attendance.scheduled_end
                    else:
                        # Last day: don't overwrite the end-of-trip timestamp
                        trip.actual_end_time = now

                    session.add(trip)
                    session.commit()

                    # Mark today's attendance present (driver fulfilled the shift)
                    trip_service.mark_trip_day_present(session, trip.id, today)

                    logger.info(f"Trip {trip.id} auto-ended")

                    # Generate daily bill
                    bill_success, bill_id, bill_error = (
                        billing_service.generate_daily_bill(session, trip.id, today)
                    )

                    if not bill_success:
                        logger.warning(
                            f"Could not generate bill for trip {trip.id}: {bill_error}"
                        )
                    elif bill_id:
                        bill = session.get(TripBill, bill_id)
                        amount = bill.total_amount if bill else 0.0
                        try:
                            send_push_notification(
                                session=session,
                                user_ids=[trip.user_id],
                                title="Trip auto-ended — bill ready",
                                body=f"Driver didn't end trip; auto-ended. Bill ₹{amount:.2f}. Pay to book your next trip.",
                                data={
                                    "type": "bill_generated",
                                    "trip_id": trip.id,
                                    "bill_id": bill_id,
                                    "amount": amount,
                                },
                            )
                            driver_user = session.exec(
                                select(User)
                                .join(Driver, Driver.user_id == User.id)
                                .where(Driver.id == trip.driver_id)
                            ).first()
                            if driver_user:
                                send_push_notification(
                                    session=session,
                                    user_ids=[driver_user.id],
                                    title="Trip auto-ended",
                                    body=f"Trip #{trip.id} auto-ended. Bill ₹{amount:.2f} sent to user.",
                                    data={
                                        "type": "bill_generated",
                                        "trip_id": trip.id,
                                        "bill_id": bill_id,
                                    },
                                )
                        except Exception:
                            pass
                else:
                    logger.error(f"Failed to auto-end trip {trip.id}: {error}")

    except Exception as e:
        logger.error(f"Trip auto-end scheduler failed: {str(e)}")


async def driver_payment_timeout_scheduler():
    """
    Background job: Auto-reject driver if payment not made in time

    Called by APScheduler at (trip_accepted_time + 30 minutes)
    """
    try:
        with Session(engine) as session:
            trip_service = TripService()

            now = now_ist()
            timeout_window = now - timedelta(minutes=30)

            # Find trips awaiting driver payment (use acceptance time, not booking time)
            trips = session.exec(
                select(Trip).where(
                    Trip.status == "accepted_pending_payment",
                    Trip.driver_payment_status == "unpaid",
                    Trip.driver_accepted_at.isnot(None),
                    Trip.driver_accepted_at <= timeout_window,
                )
            ).all()

            for trip in trips:
                # Check if driver has paid in the meantime
                payment_check = session.exec(
                    select(PaymentTransaction).where(
                        PaymentTransaction.trip_id == trip.id,
                        PaymentTransaction.driver_id == trip.driver_id,
                        PaymentTransaction.payment_status == "success",
                    )
                ).first()

                if payment_check:
                    logger.info(
                        f"Driver payment found for trip {trip.id}, skipping auto-reject"
                    )
                    continue

                # Auto-reject: bounce trip back to "searching" so allocation can re-tier
                success, error = trip_service.transition_trip_state(
                    session, trip.id, "searching", validate=True
                )

                if success:
                    trip.driver_id = None
                    trip.driver_payment_status = "unpaid"
                    session.add(trip)
                    session.commit()

                    logger.info(
                        f"Trip {trip.id} auto-rejected due to driver payment timeout; back to searching"
                    )
                    # TODO: Re-offer to next tier of drivers
                else:
                    logger.error(f"Failed to auto-reject trip {trip.id}: {error}")

    except Exception as e:
        logger.error(f"Driver payment timeout scheduler failed: {str(e)}")


async def daily_settlement_scheduler():
    """
    Background job: Generate final settlements at end of business day.
    Only runs for trips that are truly finished — terminal status AND all shift days processed
    (no more attendance rows in 'scheduled' or 'paused_payment').
    """
    try:
        with Session(engine) as session:
            billing_service = BillingService()

            today = today_ist()

            completed_trips = session.exec(
                select(Trip).where(
                    Trip.status.in_(["completed", "auto_completed"]),
                    Trip.actual_end_time.isnot(None),
                )
            ).all()

            for trip in completed_trips:
                # Skip multi-day trips that haven't reached their end_date
                if trip.end_date and today < trip.end_date:
                    continue

                # Skip if any attendance row still pending
                pending_attendance = session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == trip.id,
                        TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    )
                ).first()
                if pending_attendance:
                    continue

                # Check if settlement already generated
                existing_settlement = session.exec(
                    select(TripSettlement).where(TripSettlement.trip_id == trip.id)
                ).first()
                if existing_settlement:
                    continue

                # Generate final settlement
                success, settlement_id, error = (
                    billing_service.generate_final_settlement(session, trip.id)
                )

                if success:
                    logger.info(
                        f"Settlement generated for trip {trip.id}: {settlement_id}"
                    )
                else:
                    logger.warning(
                        f"Failed to generate settlement for trip {trip.id}: {error}"
                    )

    except Exception as e:
        logger.error(f"Daily settlement scheduler failed: {str(e)}")
