"""Background jobs for service-center bookings.

``auto_cancel_no_show_service_bookings``: a slot booking the customer never
showed up for — still ``booked`` or ``pending_confirmation`` after its slot
window has fully passed — is auto-cancelled and the slot freed. A no-show falls
inside the late-cancellation window, so the advance is forfeited (no refund),
matching the user-cancel rule (D12).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app.core.database import engine
from app.core.models import ServiceRequest, ServiceSlot, ServiceStatus
from app.utils.notifications import send_push_notification

logger = logging.getLogger("dhapp.service.scheduler")


async def auto_cancel_no_show_service_bookings() -> None:
    try:
        with Session(engine) as session:
            now = datetime.utcnow()
            # Slot bookings whose slot window has fully elapsed but are still
            # awaiting service. Walk-ins have no slot row and are skipped.
            rows = session.exec(
                select(ServiceRequest, ServiceSlot)
                .join(ServiceSlot, ServiceSlot.id == ServiceRequest.slot_id)
                .where(
                    ServiceRequest.status.in_(
                        [
                            ServiceStatus.BOOKED,
                            ServiceStatus.PENDING_CONFIRMATION,
                        ]
                    ),
                    ServiceSlot.end_time < now,
                )
            ).all()

            count = 0
            for booking, slot in rows:
                booking.status = ServiceStatus.CANCELLED.value
                booking.cancellation_time = now
                booking.cancellation_reason = "no_show (advance forfeited)"
                session.add(booking)
                session.delete(slot)  # free capacity
                count += 1
                try:
                    send_push_notification(
                        session=session,
                        user_ids=[booking.user_id],
                        title="Booking auto-cancelled",
                        body=(
                            "Your service booking was auto-cancelled as a no-show. "
                            "The advance is non-refundable."
                        ),
                        data={
                            "booking_id": booking.reference_id,
                            "type": "no_show_cancel",
                        },
                    )
                except Exception:
                    pass

            if count:
                session.commit()
                logger.info(f"Auto-cancelled {count} no-show service booking(s).")
    except Exception as e:
        logger.error(f"Service no-show auto-cancel scheduler failed: {e}")
