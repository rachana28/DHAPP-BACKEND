"""
Scheduled Jobs for Trip Management
"""

from datetime import datetime, timedelta
import logging

from sqlmodel import Session, select

from app.core.database import engine, get_redis
from app.core.models import (
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

    # Same cadence handles the inverse case: shifts that were never started and
    # whose scheduled window has now fully ended.
    await auto_mark_missed_shifts_scheduler()


async def auto_mark_missed_shifts_scheduler():
    """Mark un-started shifts absent once their scheduled window has fully ended.

    Real-time scenario: neither the user nor the driver started a shift (no OTP
    verified) and its scheduled_end has now passed — e.g. a 4 PM shift of 7
    hours whose scheduled_end is 11 PM. The day is a no-show, marked
    skipped_by_system so it is excluded from billing.

    It is deliberately a SYSTEM skip (marked_by="system"): a forgotten shift is
    not the driver's deliberate choice, so it never counts against the driver's
    monthly skip limit (which only tallies skipped_by_driver).

    Each TripAttendance row carries its own scheduled_end, so the correct
    calendar day is always targeted: a shift running past midnight is closed by
    its own row, and the next day's row (later scheduled_end, still in the
    future) is left untouched.
    """
    try:
        with Session(engine) as session:
            trip_service = TripService()
            billing_service = BillingService()
            now = now_ist()

            missed_stubs = session.exec(
                select(TripAttendance)
                .where(
                    TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    TripAttendance.user_otp_verified == False,  # noqa: E712
                    TripAttendance.driver_otp_verified == False,  # noqa: E712
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
                # Only finalize shifts for trips still waiting on / holding a shift.
                if not trip or trip.status not in ("active_pending_otp", "paused"):
                    continue

                # Re-fetch under a row lock and re-check — verify-otp or
                # skip-day may have acted between the bulk select above and now.
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
                    session.commit()  # release the row lock
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

                # If that was the trip's last pending shift, push it to a
                # terminal state so it doesn't sit in active_pending_otp /
                # paused forever.
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

                # A pending platform intent counts as "payment in progress" too:
                # the gateway charge may settle on its webhook any moment, so we
                # must NOT yank the trip back to searching and orphan a charge.
                payment_check = session.exec(
                    select(Payment).where(
                        Payment.service_type == "trip",
                        Payment.service_id == locked_trip.id,
                        Payment.payer_driver_id == locked_trip.driver_id,
                        Payment.purpose == "driver_acceptance",
                        Payment.status.in_(["pending", "succeeded"]),
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


async def auto_resolve_paused_trips_scheduler():
    """Resolve trips that have been stuck in `paused` for too long (Issue 8).

    Two real-time scenarios:

      * advance_20 / full_payment upfront unpaid — the trip is paused waiting
        for the user's upfront payment. After 1 hour the payment method is
        auto-converted to trip_day and the trip is unpaused, so it can proceed
        on pay-per-day billing instead of being stuck forever.

      * trip_day with an unpaid daily bill — the trip is paused waiting for the
        user to clear the daily bill. After 48 hours the whole booking is
        force-closed: the remaining shifts are voided and a final settlement is
        raised for the PENDING amount only. Once the user pays that settlement
        the booking is fully settled — it does NOT continue with the other
        days in the booking.

    Pause start-times are derived without any new DB column:
      * upfront pause  -> a Redis anchor key (1-hour grace) is stamped the
        first time this job sees the paused trip; if Redis is unavailable the
        conversion is simply skipped (safe degradation).
      * daily-bill pause -> the oldest unpaid daily bill's generated_at is the
        anchor (the bill is raised exactly when the pause begins).
    """
    try:
        with Session(engine) as session:
            trip_service = TripService()
            billing_service = BillingService()
            redis_client = get_redis()
            now = now_ist()

            # ── 8d: advance/full upfront pending > 1h → convert to trip_day ──
            upfront_paused = session.exec(
                select(Trip).where(
                    Trip.status == "paused",
                    Trip.is_payment_blocked == True,  # noqa: E712
                    Trip.payment_method.in_(["advance_20", "full_payment"]),
                )
            ).all()

            for trip in upfront_paused:
                anchor_key = f"trip:{trip.id}:upfront_pause_at"
                started_iso = None
                if redis_client:
                    try:
                        started_iso = redis_client.get(anchor_key)
                    except Exception:
                        started_iso = None

                if not started_iso:
                    # First sighting — stamp the 1-hour grace anchor and wait.
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
                    session.commit()  # release the row lock
                    continue

                locked.payment_method = "trip_day"
                locked.is_payment_blocked = False
                if trip_service.has_pending_shifts(session, locked.id):
                    locked.status = "active_pending_otp"
                else:
                    locked.status = "completed"
                locked.state_version += 1
                session.add(locked)

                # Re-arm any payment-paused shifts so OTP can resume.
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

            # ── 8c: trip_day unpaid daily bill paused > 48h → force-close ───
            billbased_paused = session.exec(
                select(Trip).where(
                    Trip.status == "paused",
                    Trip.payment_method == "trip_day",
                )
            ).all()

            for trip in billbased_paused:
                oldest_unpaid = session.exec(
                    select(TripBill)
                    .where(
                        TripBill.trip_id == trip.id,
                        TripBill.bill_type == "daily_bill",
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
                    session.commit()  # release the row lock
                    continue

                # Void every remaining shift — the whole booking ends here.
                for att in session.exec(
                    select(TripAttendance).where(
                        TripAttendance.trip_id == locked.id,
                        TripAttendance.status.in_(["scheduled", "paused_payment"]),
                    )
                ).all():
                    att.status = "skipped_by_system"
                    att.skip_reason = (
                        "Booking closed: daily bill unpaid for over 48 hours"
                    )
                    att.marked_by = "system"
                    session.add(att)

                if locked.actual_end_time is None:
                    locked.actual_end_time = now
                locked.status = "billed"
                locked.is_payment_blocked = False
                locked.state_version += 1
                session.add(locked)
                session.commit()

                # Final settlement carries the PENDING amount only:
                # remaining_due = billed total − amount already paid.
                ok, sid, err = billing_service.generate_final_settlement(
                    session, locked.id
                )
                logger.info(
                    f"Trip {locked.id}: daily bill unpaid for 48h — booking "
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
                            f"Trip #{locked.id} was closed because the daily "
                            f"bill stayed unpaid for 48 hours. Pay the pending "
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


# Dunning ladder (F8). Day offsets are measured from settlement.due_date
# (falling back to settlement_date if due_date is unset). Each entry is the
# stage marker we set on advance, plus the push title/body shown to the user.
# Stage 6 is the collections handoff and skips the push because the SupportTicket
# adapter handles that side.
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
    (30, 6, None, None),  # collections handoff
]


async def dunning_scheduler():
    """Run nightly (~09:00 IST). Advance overdue settlements down the dunning
    ladder, push reminders to the user, and hand off to collections at 30 days.
    """
    try:
        with Session(engine) as session:
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

                # Pick the latest ladder entry the settlement has reached but
                # not yet been advanced past. Iterate in order — the loop
                # advances at most one stage per nightly run so push fatigue
                # stays bounded.
                target_stage = None
                for threshold_days, stage, title, body in _DUNNING_LADDER:
                    if days_overdue < threshold_days:
                        break
                    if stage <= settlement.dunning_stage:
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
    except Exception as e:
        logger.error(f"Dunning scheduler failed: {str(e)}")
