"""Scheduled jobs for trip management.

Interval scans (all re-entrant — they read pending state from the DB, so a
restart after downtime resumes exactly where things stand):
  * generate_otp_for_trip_scheduler — issues the shift OTP for any runnable
    shift whose start is within 30 min OR already past (late start after an
    outage) while its scheduled_end is still in the future; pauses the trip
    instead when an unpaid blocking bill exists.
  * expire_otp_for_trip_scheduler / purge_otps_scheduler — mark missed shifts
    absent and clean up dead OTP rows.
  * auto_end_trip_scheduler (+ auto_mark_missed_shifts_scheduler) — close
    shifts past scheduled_end, raise the daily bill, run the advance_20
    mid-trip recovery check, pause on unpaid bills, settle finished trips.
  * driver_payment_timeout_scheduler — free trips whose acceptance fee never
    arrived; auto_resolve_paused_trips_scheduler — 1h unpaid upfront converts
    to trip_day, 48h unpaid bill force-closes the booking.
  * repair_orphan_active_trips_scheduler — self-heal trips with no attendance.

Daily jobs (gated, not cron): daily_settlement_scheduler (23:59) and
dunning_scheduler (09:00) run on a 10-min interval but only execute when
``daily_job_due`` says the day's slot is unserved (marker persisted as a
SystemConfig row), so a run missed during downtime executes once on restart.
"""

from datetime import datetime, timedelta
import logging

from sqlmodel import Session, select

from app.core.database import engine, get_redis
from app.core.models import (
    BookingOTP,
    Trip,
    TripOffer,
    OTPRegistry,
    Payment,
    TripAttendance,
    TripBill,
    TripSettlement,
    User,
    Driver,
)
from app.modules.trips.otp_service import OTPService
from app.modules.trips.trip_service import TripService
from app.modules.trips.billing_service import BillingService
from app.modules.trips.payment_service import (
    PaymentService,
    get_driver_abandon_suspension_hours,
)
from app.utils.system_config import daily_job_due, mark_job_run
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

            attendances = session.exec(
                select(TripAttendance).where(
                    TripAttendance.scheduled_start <= window_end,
                    TripAttendance.scheduled_end > now,
                    TripAttendance.status.in_(["scheduled", "paused_payment"]),
                )
            ).all()

            payment_service = PaymentService(redis_client)

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

                if payment_service.trip_has_unpaid_bills(session, trip.id):
                    if not trip.is_payment_blocked:
                        trip.is_payment_blocked = True
                        session.add(trip)
                    if att.status not in ("paused_payment", "skipped_by_system"):
                        att.status = "paused_payment"
                        att.skip_reason = "Outstanding bill"
                        att.marked_by = "system"
                        session.add(att)
                    session.commit()
                    continue

                existing = session.exec(
                    select(OTPRegistry).where(
                        OTPRegistry.attendance_id == att.id,
                    )
                ).first()
                if existing:
                    continue

                otp, err = otp_service.generate_otp(
                    session,
                    trip.id,
                    att.scheduled_start,
                    trip_date=att.trip_date,
                    attendance_id=att.id,
                )
                if err:
                    logger.error(
                        f"Failed to generate OTP for trip {trip.id} on {att.trip_date}: {err}"
                    )
                else:
                    logger.info(f"OTP generated for trip {trip.id} on {att.trip_date}")
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

                    try:
                        BillingService().check_advance_recovery(session, trip.id)
                    except Exception as rec_err:
                        logger.warning(
                            f"Advance-recovery check failed for trip {trip.id}: {rec_err}"
                        )

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


async def purge_otps_scheduler():
    try:
        with Session(engine) as session:
            now = now_ist()

            expired_booking = session.exec(
                select(BookingOTP).where(BookingOTP.expires_at < now)
            ).all()
            for row in expired_booking:
                session.delete(row)

            trip_cutoff = now - timedelta(hours=1)
            old_trip = session.exec(
                select(OTPRegistry).where(OTPRegistry.otp_expiry_at < trip_cutoff)
            ).all()
            for row in old_trip:
                session.delete(row)

            if expired_booking or old_trip:
                session.commit()
                logger.info(
                    f"Purged {len(expired_booking)} booking + {len(old_trip)} trip OTP rows"
                )
    except Exception as e:
        logger.error(f"OTP purge scheduler failed: {str(e)}")


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
                        TripAttendance.user_otp_verified == False,
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

                    try:
                        billing_service.check_advance_recovery(session, trip.id)
                    except Exception as rec_err:
                        logger.warning(
                            f"Advance-recovery check failed for trip {trip.id}: {rec_err}"
                        )

                    if has_future_shifts:
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

    await auto_mark_missed_shifts_scheduler()


async def auto_mark_missed_shifts_scheduler():
    try:
        with Session(engine) as session:
            trip_service = TripService()
            billing_service = BillingService()
            now = now_ist()

            missed_stubs = session.exec(
                select(TripAttendance)
                .where(
                    TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    TripAttendance.user_otp_verified == False,
                    TripAttendance.driver_otp_verified == False,
                    TripAttendance.scheduled_end <= now,
                )
                .order_by(TripAttendance.trip_date)
            ).all()

            if not missed_stubs:
                return

            trip_ids = list({a.trip_id for a in missed_stubs})
            trips_by_id = {
                t.id: t
                for t in session.exec(select(Trip).where(Trip.id.in_(trip_ids))).all()
            }

            for stub in missed_stubs:
                trip = trips_by_id.get(stub.trip_id)
                if not trip or trip.status not in ("active_pending_otp", "paused"):
                    continue

                att = session.exec(
                    select(TripAttendance)
                    .where(TripAttendance.id == stub.id)
                    .with_for_update()
                ).first()
                if (
                    not att
                    or att.status not in ("scheduled", "paused_payment")
                    or att.user_otp_verified
                    or att.driver_otp_verified
                    or att.scheduled_end > now
                ):
                    session.commit()
                    continue

                att.status = "skipped_by_system"
                att.skip_reason = "Shift not started before its scheduled end time"
                att.marked_by = "system"
                session.add(att)
                session.commit()

                logger.info(
                    f"Trip {trip.id}: shift {att.trip_date} auto-marked absent "
                    f"(not started by scheduled end {att.scheduled_end})"
                )

                if trip.driver_id:
                    used = trip_service.count_driver_skips_for_trip_in_window(
                        session, trip.id, att.trip_date
                    )
                    if used >= trip_service.DRIVER_SKIP_LIMIT_PER_TRIP_WINDOW:
                        driver = session.exec(
                            select(Driver)
                            .where(Driver.id == trip.driver_id)
                            .with_for_update()
                        ).first()
                        now_suspend = now_ist()
                        if driver and (
                            driver.suspended_until is None
                            or driver.suspended_until < now_suspend
                        ):
                            hours = get_driver_abandon_suspension_hours(
                                session, get_redis()
                            )
                            driver.suspended_until = now_suspend + timedelta(
                                hours=hours
                            )
                            session.add(driver)
                            session.commit()
                            logger.info(
                                "Driver %s suspended %sh after exceeding the "
                                "no-show/skip cap on trip %s",
                                driver.id,
                                hours,
                                trip.id,
                            )
                            try:
                                driver_user = session.get(User, driver.user_id)
                                if driver_user:
                                    send_push_notification(
                                        session=session,
                                        user_ids=[driver_user.id],
                                        title="Account temporarily suspended",
                                        body=(
                                            "You've exceeded the allowed number of "
                                            "missed/skipped shifts. New bookings "
                                            f"are paused for {int(hours)}h."
                                        ),
                                        data={
                                            "type": "driver_suspended",
                                            "trip_id": trip.reference_id,
                                        },
                                    )
                            except Exception:
                                pass

                try:
                    billing_service.check_advance_recovery(session, trip.id)
                except Exception as rec_err:
                    logger.warning(
                        f"Advance-recovery check failed for trip {trip.id}: {rec_err}"
                    )

                if trip_service.has_pending_shifts(session, trip.id):
                    continue

                fresh = session.get(Trip, trip.id)
                if not fresh or fresh.status not in (
                    "active_pending_otp",
                    "paused",
                    "ongoing",
                ):
                    continue

                has_any_present = session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == trip.id,
                        TripAttendance.status == "present",
                    )
                ).first()
                target = "completed" if has_any_present else "skipped"
                success, error = trip_service.transition_trip_state(
                    session, trip.id, target, validate=False
                )
                if not success:
                    logger.warning(
                        f"Failed to close trip {trip.id} after missed shift: {error}"
                    )
                    continue

                closed = session.get(Trip, trip.id)
                if closed and closed.actual_end_time is None:
                    closed.actual_end_time = now
                    session.add(closed)
                    session.commit()

                try:
                    billing_service.generate_final_settlement(session, trip.id)
                except Exception as settle_err:
                    logger.warning(
                        f"Inline settlement after missed-shift close failed "
                        f"for trip {trip.id}: {settle_err}"
                    )
    except Exception as e:
        logger.error(f"Missed-shift auto-absent scheduler failed: {str(e)}")


DRIVER_FEE_PENDING_GRACE_MIN = 10


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
                locked_trip = session.exec(
                    select(Trip).where(Trip.id == trip.id).with_for_update()
                ).first()
                if not locked_trip or locked_trip.status != "accepted_pending_payment":
                    continue

                grace_cutoff = now - timedelta(minutes=DRIVER_FEE_PENDING_GRACE_MIN)
                paid_fee = session.exec(
                    select(Payment).where(
                        Payment.service_type == "trip",
                        Payment.service_id == locked_trip.id,
                        Payment.payer_driver_id == locked_trip.driver_id,
                        Payment.purpose == "driver_acceptance",
                        Payment.status == "succeeded",
                    )
                ).first()
                recent_pending_fee = session.exec(
                    select(Payment).where(
                        Payment.service_type == "trip",
                        Payment.service_id == locked_trip.id,
                        Payment.payer_driver_id == locked_trip.driver_id,
                        Payment.purpose == "driver_acceptance",
                        Payment.status == "pending",
                        Payment.created_at >= grace_cutoff,
                    )
                ).first()

                if paid_fee or recent_pending_fee:
                    logger.info(
                        f"Driver fee in progress for trip {locked_trip.id}, "
                        f"skipping auto-reject"
                    )
                    session.commit()
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
                    session.commit()
                    logger.error(
                        f"Failed to auto-reject trip {locked_trip.id}: {error}"
                    )

    except Exception as e:
        logger.error(f"Driver payment timeout scheduler failed: {str(e)}")


async def daily_settlement_scheduler():
    try:
        with Session(engine) as session:
            if not daily_job_due(session, "job_last_run_daily_settlement", 23, 59):
                return

            billing_service = BillingService()

            today = today_ist()

            stmt = (
                select(Trip)
                .outerjoin(TripSettlement, TripSettlement.trip_id == Trip.id)
                .where(
                    # "skipped" included so an all-skipped trip whose settlement
                    # refund failed (and therefore has no settlement row yet) is
                    # retried here until the refund succeeds.
                    Trip.status.in_(["completed", "auto_completed", "skipped"]),
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

            mark_job_run(session, "job_last_run_daily_settlement")

    except Exception as e:
        logger.error(f"Daily settlement scheduler failed: {str(e)}")


async def auto_resolve_paused_trips_scheduler():
    try:
        with Session(engine) as session:
            trip_service = TripService()
            billing_service = BillingService()
            redis_client = get_redis()
            now = now_ist()

            upfront_paused = session.exec(
                select(Trip).where(
                    Trip.status == "paused",
                    Trip.is_payment_blocked == True,
                    Trip.payment_method.in_(["advance_20", "full_payment"]),
                )
            ).all()

            payment_service = PaymentService(redis_client)
            for trip in upfront_paused:
                if payment_service.trip_has_unpaid_bills(session, trip.id):
                    continue
                anchor_key = f"trip:{trip.id}:upfront_pause_at"
                started_iso = None
                if redis_client:
                    try:
                        started_iso = redis_client.get(anchor_key)
                    except Exception:
                        started_iso = None

                if not started_iso:
                    if redis_client:
                        try:
                            redis_client.set(anchor_key, now.isoformat(), ex=86400)
                        except Exception:
                            pass
                    continue

                try:
                    started = datetime.fromisoformat(started_iso)
                except ValueError:
                    started = now
                if (now - started) < timedelta(hours=1):
                    continue

                locked = session.exec(
                    select(Trip).where(Trip.id == trip.id).with_for_update()
                ).first()
                if (
                    not locked
                    or locked.status != "paused"
                    or locked.payment_method not in ("advance_20", "full_payment")
                    or not locked.is_payment_blocked
                ):
                    session.commit()
                    continue

                locked.payment_method = "trip_day"
                locked.is_payment_blocked = False
                if trip_service.has_pending_shifts(session, locked.id):
                    locked.status = "active_pending_otp"
                else:
                    locked.status = "completed"
                locked.state_version += 1
                session.add(locked)

                for att in session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == locked.id,
                        TripAttendance.status == "paused_payment",
                    )
                ).all():
                    att.status = "scheduled"
                    att.skip_reason = None
                    att.marked_by = "system"
                    session.add(att)
                session.commit()

                if redis_client:
                    try:
                        redis_client.delete(anchor_key)
                    except Exception:
                        pass

                logger.info(
                    f"Trip {locked.id}: advance/full upfront unpaid for 1h — "
                    f"auto-converted to trip_day and unpaused"
                )
                try:
                    send_push_notification(
                        session=session,
                        user_ids=[locked.user_id],
                        title="Switched to pay-per-day billing",
                        body=(
                            f"Upfront payment for trip #{locked.id} wasn't "
                            f"completed in time, so billing was switched to "
                            f"trip-day. You can request your OTP now."
                        ),
                        data={
                            "type": "payment_method_auto_converted",
                            "trip_id": locked.id,
                        },
                    )
                except Exception:
                    pass

            billbased_paused = session.exec(
                select(Trip).where(
                    Trip.status == "paused",
                    Trip.payment_method.in_(["trip_day", "advance_20"]),
                )
            ).all()

            for trip in billbased_paused:
                oldest_unpaid = session.exec(
                    select(TripBill)
                    .where(
                        TripBill.trip_id == trip.id,
                        TripBill.bill_type.in_(
                            ["daily_bill", "schedule_diff", "advance_recovery"]
                        ),
                        TripBill.amount_due > 0,
                    )
                    .order_by(TripBill.generated_at)
                ).first()
                if not oldest_unpaid:
                    continue
                if (now - oldest_unpaid.generated_at) < timedelta(hours=48):
                    continue

                locked = session.exec(
                    select(Trip).where(Trip.id == trip.id).with_for_update()
                ).first()
                if not locked or locked.status != "paused":
                    session.commit()
                    continue

                for att in session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == locked.id,
                        TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    )
                ).all():
                    att.status = "skipped_by_system"
                    att.skip_reason = "Booking closed: bill unpaid for over 48 hours"
                    att.marked_by = "system"
                    session.add(att)

                if locked.actual_end_time is None:
                    locked.actual_end_time = now
                locked.status = "billed"
                locked.is_payment_blocked = False
                locked.state_version += 1
                session.add(locked)
                session.commit()

                ok, sid, err = billing_service.generate_final_settlement(
                    session, locked.id
                )
                logger.info(
                    f"Trip {locked.id}: bill unpaid for 48h — booking "
                    f"force-closed to `billed`; final settlement={sid} ({err})"
                )
                try:
                    settlement = session.get(TripSettlement, sid) if sid else None
                    due = settlement.remaining_due if settlement else 0.0
                    send_push_notification(
                        session=session,
                        user_ids=[locked.user_id],
                        title="Trip closed — final bill due",
                        body=(
                            f"Trip #{locked.id} was closed because a bill "
                            f"stayed unpaid for 48 hours. Pay the pending "
                            f"amount of ₹{due:.2f} to settle the booking."
                        ),
                        data={
                            "type": "settlement_generated",
                            "trip_id": locked.id,
                            "settlement_id": sid,
                            "amount": due,
                        },
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Paused-trip resolver scheduler failed: {str(e)}")


_DUNNING_LADDER = [
    (1, 1, "Payment reminder", "Your trip settlement is due. Please clear it today."),
    (3, 2, "Payment reminder", "Your trip settlement is 3 days overdue."),
    (
        7,
        3,
        "Action required",
        "Your trip settlement is a week overdue. Please pay to keep your account active.",
    ),
    (
        14,
        4,
        "Final reminder",
        "Your trip settlement is two weeks overdue. Pay before this is escalated.",
    ),
    (
        28,
        5,
        "Pre-collections notice",
        "Your settlement will be sent to collections in 2 days if unpaid.",
    ),
    (30, 6, None, None),
]


async def dunning_scheduler():
    try:
        with Session(engine) as session:
            if not daily_job_due(session, "job_last_run_dunning", 9, 0):
                return

            today = today_ist()
            unpaid = session.exec(
                select(TripSettlement).where(
                    TripSettlement.user_payment_status != "paid",
                    TripSettlement.remaining_due > 0,
                )
            ).all()

            from app.services.collections import send_to_collections

            for settlement in unpaid:
                reference = settlement.due_date or settlement.settlement_date
                if reference is None:
                    continue
                days_overdue = (today - reference).days
                if days_overdue <= 0:
                    continue

                current_stage = settlement.dunning_stage or 0
                target_stage = None
                for threshold_days, stage, title, body in _DUNNING_LADDER:
                    if days_overdue < threshold_days:
                        break
                    if stage <= current_stage:
                        continue
                    target_stage = (threshold_days, stage, title, body)
                    break

                if not target_stage:
                    continue
                threshold_days, stage, title, body = target_stage

                if stage == 6:
                    send_to_collections(session, settlement, days_overdue)
                else:
                    try:
                        send_push_notification(
                            session=session,
                            user_ids=[settlement.user_id],
                            title=title,
                            body=body,
                            data={
                                "type": "settlement_overdue",
                                "trip_id": settlement.trip_id,
                                "settlement_id": settlement.id,
                                "days_overdue": days_overdue,
                            },
                        )
                    except Exception:
                        pass

                settlement.dunning_stage = stage
                settlement.last_reminder_at = now_ist()
                session.add(settlement)
                session.commit()

            mark_job_run(session, "job_last_run_dunning")
    except Exception as e:
        logger.error(f"Dunning scheduler failed: {str(e)}")


async def repair_orphan_active_trips_scheduler():
    try:
        with Session(engine) as session:
            trip_service = TripService()
            orphans = session.exec(
                select(Trip)
                .outerjoin(TripAttendance, TripAttendance.trip_id == Trip.id)
                .where(
                    Trip.status == "active_pending_otp",
                    Trip.driver_payment_status == "paid",
                    Trip.start_date.isnot(None),
                    TripAttendance.id.is_(None),
                )
            ).all()

            for trip in orphans:
                duration_hours = (
                    trip_service.get_trip_duration_hours(trip.shift_details)
                    or trip.trip_duration_hours
                    or 8
                )
                parsed_time = trip_service.get_trip_start_time(
                    trip.shift_details, trip.start_date
                )
                effective_start_dt = (
                    parsed_time
                    or trip.scheduled_start_time
                    or datetime.combine(trip.start_date, datetime.min.time())
                )
                is_outstation = (trip.hiring_type or "").strip().lower() == "outstation"
                end_date_for_attendance = trip.end_date or trip.start_date

                ok, err = trip_service.create_trip_attendance_records(
                    session,
                    trip.id,
                    trip.start_date,
                    end_date_for_attendance,
                    effective_start_dt,
                    duration_hours,
                    selected_days=trip.selected_days,
                    single_shift=is_outstation,
                    commit=False,
                )
                if ok:
                    if is_outstation:
                        single_att = session.exec(
                            select(TripAttendance).where(
                                TripAttendance.trip_id == trip.id
                            )
                        ).first()
                        if single_att:
                            trip.scheduled_start_time = single_att.scheduled_start
                            trip.scheduled_end_time = single_att.scheduled_end
                            session.add(trip)
                    session.commit()
                    logger.info(
                        f"Repaired orphan trip {trip.id}: regenerated attendance rows."
                    )
                else:
                    session.rollback()
                    logger.warning(f"Orphan trip {trip.id} repair failed: {err}")
    except Exception as e:
        logger.error(f"Orphan-trip repair scheduler failed: {str(e)}")
