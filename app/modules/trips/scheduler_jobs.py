"""
Scheduled Jobs for Trip Management
"""

from datetime import timedelta
import logging

from sqlmodel import Session, select

from app.core.database import engine, get_redis
from app.core.models import (
    Trip,
    TripOffer,
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
    try:
        with Session(engine) as session:
            redis_client = get_redis()
            otp_service = OTPService(redis_client)

            now = now_ist()
            window_end = now + timedelta(minutes=30)
            today = today_ist()

            attendances = session.exec(
                select(TripAttendance).where(
                    TripAttendance.scheduled_start >= now,
                    TripAttendance.scheduled_start <= window_end,
                    TripAttendance.status.notin_(
                        ["skipped_by_user", "skipped_by_driver", "skipped_by_system"]
                    ),
                )
            ).all()

            payment_service = PaymentService(redis_client)

            # Batch-fetch trips to avoid N+1.
            trip_ids = list({a.trip_id for a in attendances})
            trips_by_id = {
                t.id: t
                for t in (
                    session.exec(select(Trip).where(Trip.id.in_(trip_ids))).all()
                    if trip_ids
                    else []
                )
            }

            for att in attendances:
                trip = trips_by_id.get(att.trip_id)
                if not trip or trip.status not in (
                    "active_pending_otp",
                    "ongoing",
                    "active",
                ):
                    continue

                if not trip.payment_method:
                    continue

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
                    continue

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
                    try:
                        send_push_notification(
                            session=session,
                            user_ids=[trip.user_id],
                            title="Trip OTP Ready",
                            body=f"Your trip OTP is ready. Open the app to view it and share with your driver to start trip #{trip.id}.",
                            data={
                                "type": "trip_otp",
                                "trip_id": trip.id,
                            },
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"OTP generation scheduler failed: {str(e)}")


async def expire_otp_for_trip_scheduler():
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

            trip_service = TripService()

            # Batch-fetch trips to avoid N+1.
            trip_ids = list({r.trip_id for r in expired_rows})
            trips_by_id = {
                t.id: t
                for t in (
                    session.exec(select(Trip).where(Trip.id.in_(trip_ids))).all()
                    if trip_ids
                    else []
                )
            }

            for row in expired_rows:
                trip = trips_by_id.get(row.trip_id)
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

                    # If this was the trip's last pending shift, advance the
                    # trip status. Without this the trip stays in
                    # active_pending_otp forever after an OTP timeout.
                    if not trip_service.has_pending_shifts(session, trip.id):
                        has_any_present = session.exec(
                            select(TripAttendance).where(
                                TripAttendance.trip_id == trip.id,
                                TripAttendance.status == "present",
                            )
                        ).first()
                        target = "completed" if has_any_present else "skipped"
                        trip_service.transition_trip_state(
                            session, trip.id, target, validate=False
                        )

                otp_service.invalidate_otp(trip.id, row.trip_date)
                logger.info(
                    f"Trip {trip.id} OTP expired for {row.trip_date}; attendance marked missed"
                )
    except Exception as e:
        logger.error(f"OTP expiry scheduler failed: {str(e)}")


async def auto_end_trip_scheduler():
    try:
        with Session(engine) as session:
            trip_service = TripService()
            billing_service = BillingService()

            now = now_ist()

            candidate_ids = [
                row.id
                for row in session.exec(
                    select(Trip.id).where(
                        Trip.status == "ongoing",
                        Trip.scheduled_end_time <= now,
                        Trip.actual_end_time.is_(None),
                    )
                ).all()
            ]

            for trip_id in candidate_ids:
                # Re-fetch under a row lock and re-check state — driver may
                # have called /end-trip in parallel between the bulk select
                # above and now.
                trip = session.exec(
                    select(Trip).where(Trip.id == trip_id).with_for_update()
                ).first()
                if not trip or trip.status != "ongoing":
                    session.commit()
                    continue

                active_attendance = session.exec(
                    select(TripAttendance)
                    .where(
                        TripAttendance.trip_id == trip.id,
                        TripAttendance.user_otp_verified,
                        TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    )
                    .order_by(TripAttendance.trip_date.desc())
                ).first()

                shift_date = (
                    active_attendance.trip_date if active_attendance else today_ist()
                )

                next_attendance = session.exec(
                    select(TripAttendance)
                    .where(
                        TripAttendance.trip_id == trip.id,
                        TripAttendance.trip_date > shift_date,
                        TripAttendance.status.in_(["scheduled", "paused_payment"]),
                        TripAttendance.user_otp_verified == False,  # noqa: E712
                    )
                    .order_by(TripAttendance.trip_date)
                ).first()
                has_future_shifts = next_attendance is not None

                next_state = (
                    "active_pending_otp" if has_future_shifts else "auto_completed"
                )
                success, error = trip_service.transition_trip_state(
                    session, trip.id, next_state, validate=True
                )

                if success:
                    if has_future_shifts:
                        trip.scheduled_start_time = next_attendance.scheduled_start
                        trip.scheduled_end_time = next_attendance.scheduled_end
                    else:
                        trip.actual_end_time = now

                    session.add(trip)
                    session.commit()

                    # Stamp per-day actual_end so summaries reflect each shift.
                    if active_attendance:
                        trip_service.mark_trip_day_present(
                            session, trip.id, shift_date, actual_end=now
                        )

                    logger.info(f"Trip {trip.id} auto-ended")

                    bill_success, bill_id, bill_error = (
                        billing_service.generate_daily_bill(
                            session, trip.id, shift_date
                        )
                    )

                    if not bill_success and bill_id:
                        bill_success = True

                    # trip_day + future shifts with an unpaid bill: hold the
                    # trip in `paused` so the user app surfaces the payment
                    # screen instead of jumping to tomorrow's OTP screen.
                    # Cleared by PaymentService.unpause_trip_if_clear once
                    # the bill is paid.
                    if has_future_shifts and trip.payment_method == "trip_day":
                        payment_service = PaymentService(get_redis())
                        if payment_service.trip_has_unpaid_bills(session, trip.id):
                            trip.status = "paused"
                            trip.is_payment_blocked = True
                            trip.state_version += 1
                            session.add(trip)
                            session.commit()

                    if not has_future_shifts:
                        try:
                            billing_service.generate_final_settlement(session, trip.id)
                        except Exception as settle_err:
                            logger.warning(
                                f"Inline settlement generation failed for trip {trip.id}: {settle_err}"
                            )

                    if not bill_success:
                        logger.warning(
                            f"Could not generate bill for trip {trip.id}: {bill_error}"
                        )
                    elif bill_id:
                        bill = session.get(TripBill, bill_id)
                        amount = bill.amount_due if bill else 0.0
                        try:
                            if amount > 0:
                                body = f"Driver didn't end trip; auto-ended. Bill due: ₹{amount:.2f}. Pay to book your next trip."
                            else:
                                body = "Driver didn't end trip; auto-ended. Bill was already paid."

                            send_push_notification(
                                session=session,
                                user_ids=[trip.user_id],
                                title="Trip auto-ended — bill ready",
                                body=body,
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
                                    body=f"Trip #{trip.id} auto-ended. Bill sent to user.",
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
    try:
        with Session(engine) as session:
            trip_service = TripService()

            now = now_ist()
            timeout_window = now - timedelta(minutes=30)

            trips = session.exec(
                select(Trip).where(
                    Trip.status == "accepted_pending_payment",
                    Trip.driver_payment_status == "unpaid",
                    Trip.driver_accepted_at.isnot(None),
                    Trip.driver_accepted_at <= timeout_window,
                )
            ).all()

            for trip in trips:
                # Re-fetch with a row lock and re-check status: the driver
                # may have just paid in /process-payment, or the user may
                # have just cancelled, between the bulk select above and now.
                locked_trip = session.exec(
                    select(Trip).where(Trip.id == trip.id).with_for_update()
                ).first()
                if not locked_trip or locked_trip.status != "accepted_pending_payment":
                    continue

                payment_check = session.exec(
                    select(PaymentTransaction).where(
                        PaymentTransaction.trip_id == locked_trip.id,
                        PaymentTransaction.driver_id == locked_trip.driver_id,
                        PaymentTransaction.payment_status == "success",
                    )
                ).first()

                if payment_check:
                    logger.info(
                        f"Driver payment found for trip {locked_trip.id}, skipping auto-reject"
                    )
                    session.commit()  # release the row lock
                    continue

                success, error = trip_service.transition_trip_state(
                    session, locked_trip.id, "searching", validate=True
                )

                if success:
                    timed_out_driver_id = locked_trip.driver_id
                    locked_trip.driver_id = None
                    locked_trip.driver_payment_status = "unpaid"
                    stale_offer = session.exec(
                        select(TripOffer).where(
                            TripOffer.trip_id == locked_trip.id,
                            TripOffer.driver_id == timed_out_driver_id,
                        )
                    ).first()
                    if stale_offer:
                        stale_offer.status = "rejected"
                        session.add(stale_offer)
                    session.add(locked_trip)
                    session.commit()

                    try:
                        from app.modules.trips.allocation import (
                            attempt_trip_escalation,
                        )

                        if attempt_trip_escalation(session, locked_trip):
                            session.commit()
                    except Exception as esc_err:
                        logger.warning(
                            f"Re-allocation after payment timeout failed for trip {locked_trip.id}: {esc_err}"
                        )

                    logger.info(
                        f"Trip {locked_trip.id} auto-rejected due to driver payment timeout; back to searching"
                    )
                else:
                    session.commit()  # release the row lock
                    logger.error(
                        f"Failed to auto-reject trip {locked_trip.id}: {error}"
                    )

    except Exception as e:
        logger.error(f"Driver payment timeout scheduler failed: {str(e)}")


async def daily_settlement_scheduler():
    try:
        with Session(engine) as session:
            billing_service = BillingService()

            today = today_ist()

            # Filter at the DB level: only trips that are completed/auto_completed,
            # have ended, are past end_date (or end_date is null), and do NOT
            # already have a settlement. Avoids loading every historical trip
            # on every cron tick.
            stmt = (
                select(Trip)
                .outerjoin(TripSettlement, TripSettlement.trip_id == Trip.id)
                .where(
                    Trip.status.in_(["completed", "auto_completed"]),
                    Trip.actual_end_time.isnot(None),
                    TripSettlement.id.is_(None),
                )
            )
            completed_trips = session.exec(stmt).all()

            for trip in completed_trips:
                if trip.end_date and today < trip.end_date:
                    continue

                pending_attendance = session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == trip.id,
                        TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    )
                ).first()
                if pending_attendance:
                    continue

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
