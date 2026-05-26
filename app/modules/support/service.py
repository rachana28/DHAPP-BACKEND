import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.models import (
    SupportTicket,
    SupportTicketCreate,
    SupportMessage,
    SupportAttachment,
    User,
    Trip,
    TowTrip,
    MechanicTrip,
    ServiceRequest,
    Driver,
    TowTruckDriver,
    Mechanic,
    SUPPORT_SERVICE_TYPES,
    SUPPORT_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Service snapshot builders
# ---------------------------------------------------------------------------


def _trip_snapshot(trip: Trip) -> Dict[str, Any]:
    return {
        "service_type": "trip",
        "trip_id": trip.id,
        "status": trip.status,
        "hiring_type": trip.hiring_type,
        "vehicle_type": trip.vehicle_type,
        "booking_time": str(trip.booking_time) if trip.booking_time else None,
        "scheduled_start_time": str(trip.scheduled_start_time)
        if trip.scheduled_start_time
        else None,
        "scheduled_end_time": str(trip.scheduled_end_time)
        if trip.scheduled_end_time
        else None,
        "actual_start_time": str(trip.actual_start_time)
        if trip.actual_start_time
        else None,
        "actual_end_time": str(trip.actual_end_time) if trip.actual_end_time else None,
        "driver_id": trip.driver_id,
        "user_id": str(trip.user_id) if trip.user_id else None,
        "start_location": trip.start_location,
        "end_location": trip.end_location,
        "fare": trip.fare,
    }


def _tow_snapshot(trip: TowTrip) -> Dict[str, Any]:
    return {
        "service_type": "tow",
        "tow_trip_id": trip.id,
        "status": trip.status,
        "vehicle_type": trip.vehicle_type,
        "booking_time": str(trip.booking_time) if trip.booking_time else None,
        "tow_truck_driver_id": trip.tow_truck_driver_id,
        "user_id": str(trip.user_id) if trip.user_id else None,
        "start_location": trip.start_location,
        "end_location": trip.end_location,
        "start_lat": trip.start_lat,
        "start_lng": trip.start_lng,
        "end_lat": trip.end_lat,
        "end_lng": trip.end_lng,
        "distance_km": trip.distance_km,
        "fare": trip.fare,
        "reason": trip.reason,
    }


def _mechanic_snapshot(trip: MechanicTrip) -> Dict[str, Any]:
    return {
        "service_type": "mechanic",
        "mechanic_trip_id": trip.id,
        "status": trip.status,
        "vehicle_type": trip.vehicle_type,
        "booking_time": str(trip.booking_time) if trip.booking_time else None,
        "mechanic_id": trip.mechanic_id,
        "user_id": str(trip.user_id) if trip.user_id else None,
        "start_location": trip.start_location,
        "start_lat": trip.start_lat,
        "start_lng": trip.start_lng,
        "reason": trip.reason,
        "fare": trip.fare,
    }


def _service_center_snapshot(req: ServiceRequest) -> Dict[str, Any]:
    return {
        "service_type": "service_center",
        "service_request_id": req.id,
        "status": req.status.value if hasattr(req.status, "value") else req.status,
        "service_center_id": req.service_center_id,
        "center_service_id": req.center_service_id,
        "service_name": req.service_name,
        "vehicle_type": req.vehicle_type,
        "vehicle_number": req.vehicle_number,
        "booking_time": str(req.booking_time) if req.booking_time else None,
        "requested_date": str(req.requested_date) if req.requested_date else None,
        "requested_time": req.requested_time,
        "expected_return_date": str(req.expected_return_date)
        if req.expected_return_date
        else None,
        "expected_return_time": req.expected_return_time,
        "user_id": str(req.user_id) if req.user_id else None,
    }


# ---------------------------------------------------------------------------
# Ownership validation
# ---------------------------------------------------------------------------


def _user_can_access_service(
    session: Session,
    current_user: User,
    service_type: str,
    service_ref_id: int,
) -> Tuple[bool, Optional[Any]]:
    """
    Return (allowed, service_row). A user can raise support for a service if
    they were the customer (user_id match), or if they were the assigned
    provider (driver / tow_truck_driver / mechanic) for that booking.
    """
    if service_type == "trip":
        row = session.get(Trip, service_ref_id)
        if not row:
            return False, None
        if row.user_id == current_user.id:
            return True, row
        # Could be the assigned driver
        if current_user.role == "driver":
            drv = session.exec(
                select(Driver).where(Driver.user_id == current_user.id)
            ).first()
            if drv and row.driver_id == drv.id:
                return True, row
        return False, row

    if service_type == "tow":
        row = session.get(TowTrip, service_ref_id)
        if not row:
            return False, None
        if row.user_id == current_user.id:
            return True, row
        if current_user.role == "tow_truck_driver":
            drv = session.exec(
                select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
            ).first()
            if drv and row.tow_truck_driver_id == drv.id:
                return True, row
        return False, row

    if service_type == "mechanic":
        row = session.get(MechanicTrip, service_ref_id)
        if not row:
            return False, None
        if row.user_id == current_user.id:
            return True, row
        if current_user.role == "mechanic":
            mech = session.exec(
                select(Mechanic).where(Mechanic.user_id == current_user.id)
            ).first()
            if mech and row.mechanic_id == mech.id:
                return True, row
        return False, row

    if service_type == "service_center":
        row = session.get(ServiceRequest, service_ref_id)
        if not row:
            return False, None
        if row.user_id == current_user.id:
            return True, row
        # service_center role typically owns center, not the booking directly;
        # we don't allow service center to raise tickets on customer bookings.
        return False, row

    return False, None


def build_service_snapshot(service_type: str, row: Any) -> Optional[Dict[str, Any]]:
    if service_type == "trip" and isinstance(row, Trip):
        return _trip_snapshot(row)
    if service_type == "tow" and isinstance(row, TowTrip):
        return _tow_snapshot(row)
    if service_type == "mechanic" and isinstance(row, MechanicTrip):
        return _mechanic_snapshot(row)
    if service_type == "service_center" and isinstance(row, ServiceRequest):
        return _service_center_snapshot(row)
    return None


def fetch_live_service(
    session: Session, service_type: str, service_ref_id: int
) -> Optional[Dict[str, Any]]:
    """Re-fetch the linked service row and return a fresh snapshot dict."""
    if not service_type or not service_ref_id:
        return None
    if service_type == "trip":
        row = session.get(Trip, service_ref_id)
        return _trip_snapshot(row) if row else None
    if service_type == "tow":
        row = session.get(TowTrip, service_ref_id)
        return _tow_snapshot(row) if row else None
    if service_type == "mechanic":
        row = session.get(MechanicTrip, service_ref_id)
        return _mechanic_snapshot(row) if row else None
    if service_type == "service_center":
        row = session.get(ServiceRequest, service_ref_id)
        return _service_center_snapshot(row) if row else None
    return None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def generate_ticket_id() -> str:
    return f"TKT-{uuid.uuid4().hex[:8].upper()}"


def create_ticket(
    session: Session,
    current_user: User,
    payload: SupportTicketCreate,
) -> SupportTicket:
    if payload.category and payload.category not in SUPPORT_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of {sorted(SUPPORT_CATEGORIES)}",
        )

    service_type = payload.service_type
    service_ref_id = payload.service_ref_id
    snapshot: Optional[Dict[str, Any]] = None

    if service_type and not service_ref_id:
        raise HTTPException(
            status_code=400,
            detail="service_ref_id required when service_type is set",
        )
    if service_ref_id and not service_type:
        raise HTTPException(
            status_code=400,
            detail="service_type required when service_ref_id is set",
        )

    if service_type and service_ref_id:
        if service_type not in SUPPORT_SERVICE_TYPES:
            raise HTTPException(status_code=400, detail="invalid service_type")
        allowed, row = _user_can_access_service(
            session, current_user, service_type, service_ref_id
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="You do not have access to this service record.",
            )
        snapshot = build_service_snapshot(service_type, row)

    ticket = SupportTicket(
        subject=payload.subject,
        description=payload.description,
        category=payload.category or "general",
        priority=payload.priority or "medium",
        user_id=current_user.id,
        ticket_id=generate_ticket_id(),
        status="open",
        service_type=service_type,
        service_ref_id=service_ref_id,
        service_snapshot=snapshot,
        auto_close_enabled=bool(service_type),
        raised_by_role=current_user.role or "user",
        last_message_at=datetime.utcnow(),
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    # Persist the opening description as the first chat message so the chat
    # transcript is self-contained.
    opening = SupportMessage(
        ticket_id=ticket.id,
        sender_role=current_user.role or "user",
        sender_id=str(current_user.id),
        body=payload.description,
        is_system=False,
    )
    session.add(opening)
    session.commit()
    session.refresh(ticket)
    return ticket


def assert_can_view_ticket(ticket: SupportTicket, current_user: User) -> None:
    if ticket.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorised for this ticket.")


def link_attachments_to_message(
    session: Session,
    ticket_id: int,
    sender_id: str,
    attachment_ids: List[int],
    message_id: int,
) -> List[SupportAttachment]:
    """Validate that attachment_ids belong to this ticket and the sender, then
    bind them to the new message_id."""
    if not attachment_ids:
        return []
    rows = session.exec(
        select(SupportAttachment).where(
            SupportAttachment.id.in_(attachment_ids),
            SupportAttachment.ticket_id == ticket_id,
        )
    ).all()
    found_ids = {r.id for r in rows}
    missing = set(attachment_ids) - found_ids
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown attachment ids: {sorted(missing)}",
        )
    for r in rows:
        if r.message_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Attachment already linked to another message.",
            )
        if r.uploaded_by_id != sender_id:
            raise HTTPException(
                status_code=403,
                detail="Cannot attach files uploaded by another user.",
            )
        r.message_id = message_id
        session.add(r)
    session.commit()
    return rows


def append_message(
    session: Session,
    ticket: SupportTicket,
    sender_role: str,
    sender_id: str,
    body: Optional[str],
    attachment_ids: Optional[List[int]] = None,
    is_system: bool = False,
) -> SupportMessage:
    if ticket.status == "closed":
        raise HTTPException(status_code=409, detail="Ticket is closed.")
    if not body and not attachment_ids and not is_system:
        raise HTTPException(
            status_code=400,
            detail="Message must contain a body or at least one attachment.",
        )

    msg = SupportMessage(
        ticket_id=ticket.id,
        sender_role=sender_role,
        sender_id=sender_id,
        body=body,
        is_system=is_system,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)

    if attachment_ids:
        link_attachments_to_message(
            session, ticket.id, sender_id, attachment_ids, msg.id
        )
        session.refresh(msg)

    # Update ticket lifecycle
    ticket.last_message_at = datetime.utcnow()
    ticket.updated_at = datetime.utcnow()
    if ticket.status == "open" and sender_role == "admin":
        ticket.status = "in_progress"
    session.add(ticket)
    session.commit()
    session.refresh(msg)
    return msg


def close_ticket(
    session: Session,
    ticket: SupportTicket,
    closed_by: str,
    system_note: Optional[str] = None,
) -> SupportTicket:
    if ticket.status == "closed":
        return ticket
    ticket.status = "closed"
    ticket.closed_at = datetime.utcnow()
    ticket.closed_by = closed_by
    ticket.updated_at = datetime.utcnow()
    session.add(ticket)
    session.commit()

    if system_note:
        msg = SupportMessage(
            ticket_id=ticket.id,
            sender_role="system",
            sender_id="system",
            body=system_note,
            is_system=True,
        )
        session.add(msg)
        session.commit()

    session.refresh(ticket)
    return ticket


def serialize_attachment(att: SupportAttachment) -> Dict[str, Any]:
    return {
        "id": att.id,
        "ticket_id": att.ticket_id,
        "message_id": att.message_id,
        "file_url": att.file_url,
        "file_name": att.file_name,
        "file_type": att.file_type,
        "mime_type": att.mime_type,
        "file_size": att.file_size,
        "uploaded_by_role": att.uploaded_by_role,
        "created_at": att.created_at,
    }


def serialize_message(msg: SupportMessage) -> Dict[str, Any]:
    return {
        "id": msg.id,
        "ticket_id": msg.ticket_id,
        "sender_role": msg.sender_role,
        "sender_id": msg.sender_id,
        "body": None if msg.is_deleted else msg.body,
        "is_system": msg.is_system,
        "is_deleted": msg.is_deleted,
        "created_at": msg.created_at,
        "attachments": [serialize_attachment(a) for a in (msg.attachments or [])],
    }


def get_customer_user_ids(ticket: SupportTicket) -> List[uuid.UUID]:
    """Returns the user_ids that should receive a push when admin replies."""
    return [ticket.user_id] if ticket.user_id else []
