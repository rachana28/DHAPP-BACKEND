from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile, Body
from sqlmodel import Session, select, func, desc
from typing import List, Dict, Any
import redis
from datetime import datetime, date
from sqlalchemy.exc import NoResultFound
from app.core.database import get_session, get_redis
from app.core.models import (
    ServiceCenter,
    ServiceCenterUpdate,
    ServiceCenterPrivate,
    ServiceCenterPublic,
    CenterService,
    CenterServicePublic,
    CenterServiceCreate,
    CenterServiceUpdate,
    ServiceSlot,
    ServiceSlotPublic,
    ServiceSlotCreate,
    ServiceRequest,
    ServiceRequestPublic,
    ServiceSlotUpdate,
    ServiceRequestForCenter,
    BookingType,
    ServiceStatus,
    CenterMember,
    CenterMemberForCenter,
    CenterMemberAssignCandidate,
    MemberAssignmentRequest,
    MemberApprovalRequest,
)
from app.core import cache
from app.utils.booking_states import SERVICE_CENTER_ENGAGED_STATES
from app.core.security import get_current_active_service_center
from app.modules.payments.service import refund_booking_payments
from app.modules.service.booking_summary import build_service_summary
from app.modules.service import assignment as member_assignment
from app.utils.storage import upload_document_to_r2, upload_profile_picture_to_r2
from app.utils.id_generator import get_by_reference
from app.utils.notifications import notify_safe
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/service-centers", tags=["Service Centers"])


# --- PROFILE MANAGEMENT ---


def _center_me_key(current_center: ServiceCenter) -> str:
    return cache.me_key("service_center", current_center.id)


@router.get("/me", response_model=ServiceCenterPrivate)
def read_current_service_center_profile(
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Get the full profile for the currently authenticated service center.
    Functionality: Retrieve service center's own complete profile information.
    Redis-cached; invalidated on profile/picture/document writes below.
    """
    key = _center_me_key(current_center)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached
    data = ServiceCenterPrivate.model_validate(
        current_center, from_attributes=True
    ).model_dump(mode="json")
    cache.cache_set_json(key, data, cache.ME_CACHE_TTL)
    return data


@router.patch("/me", response_model=ServiceCenterPrivate)
def update_current_service_center_profile(
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    center_update: ServiceCenterUpdate,
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Update the profile for the currently authenticated service center.
    Functionality: Modify service center details (address, lat/long, etc.)
    """
    update_data = center_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_center, key, value)

    if current_center.status == "rejected":
        current_center.status = "pending_approval"

    session.add(current_center)
    session.commit()
    session.refresh(current_center)

    # Invalidate cache for updated center
    if redis_client:
        redis_client.delete(f"service_center_{current_center.id}")
    cache.cache_delete(_center_me_key(current_center))

    return current_center


@router.post("/me/documents")
async def upload_verification_document(
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    file: UploadFile = File(...),
):
    """
    Upload KYC documents, licenses, or garage photos for admin approval.
    """
    # Reuse your R2 storage utility, but change the prefix folder
    public_url = await upload_document_to_r2(
        file, "kyc_documents", str(current_center.id)
    )

    # Append the new document URL to the JSON array safely
    current_docs = current_center.verification_documents or []

    # Create a new list to trigger SQLAlchemy's JSON mutation detection
    current_center.verification_documents = [*current_docs, public_url]

    if current_center.status == "rejected":
        current_center.status = "pending_approval"

    session.add(current_center)
    session.commit()
    session.refresh(current_center)

    cache.cache_delete(_center_me_key(current_center))
    return {
        "message": "Document uploaded successfully",
        "documents": current_center.verification_documents,
    }


@router.put("/me/profile-picture", response_model=ServiceCenterPrivate)
async def update_profile_picture(
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    file: UploadFile = File(...),
):
    """
    Update the profile picture for the currently authenticated service center by uploading to Cloudflare R2.
    Functionality: Upload and set service center's profile picture
    """
    # Upload to R2 and get the public URL
    public_url = await upload_profile_picture_to_r2(
        file, "service_center", str(current_center.id)
    )

    # Save the R2 URL to the database
    current_center.profile_picture_url = public_url
    session.add(current_center)
    session.commit()
    session.refresh(current_center)

    cache.cache_delete(_center_me_key(current_center))
    return current_center


@router.get("/{center_id}", response_model=ServiceCenterPublic)
def read_service_center(
    center_id: str,
    session: Session = Depends(get_session),
):
    """
    Get details for a specific service center by ID.
    Functionality: Public endpoint to view service center profile with total bookings
    """
    center = get_by_reference(session, ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    booking_count = session.exec(
        select(func.count(ServiceRequest.id)).where(
            ServiceRequest.service_center_id == center.id
        )
    ).one()

    return ServiceCenterPublic(**center.model_dump(), total_bookings=booking_count)


# --- SERVICE MANAGEMENT ---


@router.post("/me/services", response_model=CenterServicePublic)
def add_service(
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    service_data: CenterServiceCreate,
):
    """
    Add a new custom service to the service center's offerings.
    Functionality: Service center can add custom services with flexible booking types
    """
    # Validate vehicle types are not empty
    if not service_data.vehicle_types:
        raise HTTPException(
            status_code=400,
            detail="At least one vehicle type must be specified",
        )

    # Validate slot configuration for slot-based services
    if service_data.booking_type == BookingType.SLOT_BASED:
        if service_data.service_duration_hours <= 0:
            raise HTTPException(
                status_code=400,
                detail="Service duration must be greater than 0 for slot-based services",
            )
        if (
            service_data.slot_interval_minutes <= 0
            or service_data.slot_interval_minutes > 120
        ):
            raise HTTPException(
                status_code=400,
                detail="Slot interval must be between 1 and 120 minutes",
            )
        if service_data.max_daily_bookings <= 0:
            raise HTTPException(
                status_code=400,
                detail="Max daily bookings must be greater than 0",
            )

    # Validate pricing components
    total_price = 0.0
    if service_data.pricing_components:
        for component in service_data.pricing_components:
            if "amount" not in component or component["amount"] < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Each pricing component must have a valid amount",
                )
            total_price += component["amount"]

        # Calculate price from components if not provided
        if not service_data.price:
            service_data.price = total_price

    new_service = CenterService(
        service_center_id=current_center.id, **service_data.model_dump()
    )
    session.add(new_service)
    session.commit()
    session.refresh(new_service)

    return CenterServicePublic(**new_service.model_dump())


@router.get("/me/services", response_model=List[CenterServicePublic])
def get_my_services(
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Get all services offered by the current service center.
    Functionality: Retrieve service center's service offerings
    """
    services = session.exec(
        select(CenterService).where(
            CenterService.service_center_id == current_center.id
        )
    ).all()
    return [CenterServicePublic(**s.model_dump()) for s in services]


@router.patch("/me/services/{service_id}", response_model=CenterServicePublic)
def update_service(
    service_id: int,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    service_update: CenterServiceUpdate,
):
    """
    Update service details.
    Functionality: Modify service name, duration, pricing, vehicle types, or availability
    """
    service = session.get(CenterService, service_id)
    if not service or service.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    update_data = service_update.model_dump(exclude_unset=True)

    # Validate pricing components if provided
    if "pricing_components" in update_data and update_data["pricing_components"]:
        total_price = 0.0
        for component in update_data["pricing_components"]:
            if "amount" not in component or component["amount"] < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Each pricing component must have a valid amount",
                )
            total_price += component["amount"]

        # Auto-calculate price from components
        if "price" not in update_data or update_data["price"] is None:
            update_data["price"] = total_price

    for key, value in update_data.items():
        setattr(service, key, value)

    session.add(service)
    session.commit()
    session.refresh(service)

    return CenterServicePublic(**service.model_dump())


@router.delete("/me/services/{service_id}")
def delete_service(
    service_id: int,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Delete a custom service.
    Functionality: Remove a service offering from the center.
    """
    service = session.get(CenterService, service_id)
    if not service or service.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    # Safety check: Prevent deletion if there are active bookings
    active_bookings = session.exec(
        select(ServiceRequest).where(
            ServiceRequest.center_service_id == service_id,
            ServiceRequest.status.notin_(
                [ServiceStatus.COMPLETED, ServiceStatus.CANCELLED]
            ),
        )
    ).first()

    if active_bookings:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a service that has active ongoing bookings. Please cancel or complete them first.",
        )

    # Optionally: Clean up associated slots if it's a slot-based service
    if service.booking_type == BookingType.SLOT_BASED:
        slots = session.exec(
            select(ServiceSlot).where(ServiceSlot.center_service_id == service_id)
        ).all()
        for slot in slots:
            session.delete(slot)

    session.delete(service)
    session.commit()

    return {"message": "Service deleted successfully"}


# --- SLOT MANAGEMENT (for slot-based services) ---


@router.post("/me/services/{service_id}/slots", response_model=ServiceSlotPublic)
def create_service_slot(
    service_id: int,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    slot_data: ServiceSlotCreate,
):
    """
    Create a time slot for a slot-based service.
    Functionality: Service center creates time slots for slot-based services
    """
    service = session.get(CenterService, service_id)
    if not service or service.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    # Only for slot-based services
    if service.booking_type != BookingType.SLOT_BASED:
        raise HTTPException(
            status_code=400,
            detail=f"Slots are not applicable for {service.booking_type} services",
        )

    # Validate time
    if slot_data.start_time >= slot_data.end_time:
        raise HTTPException(
            status_code=400, detail="Start time must be before end time"
        )

    overlapping_slot = session.exec(
        select(ServiceSlot).where(
            ServiceSlot.center_service_id == service_id,
            ServiceSlot.start_time < slot_data.end_time,
            ServiceSlot.end_time > slot_data.start_time,
        )
    ).first()

    if overlapping_slot:
        raise HTTPException(
            status_code=400, detail="This time slot overlaps with an existing slot."
        )

    new_slot = ServiceSlot(
        service_center_id=current_center.id,
        center_service_id=service_id,
        **slot_data.model_dump(),
    )
    session.add(new_slot)
    session.commit()
    session.refresh(new_slot)

    return ServiceSlotPublic(**new_slot.model_dump())


@router.patch(
    "/me/services/{service_id}/slots/{slot_id}", response_model=ServiceSlotPublic
)
def update_service_slot(
    service_id: int,
    slot_id: int,
    slot_update: ServiceSlotUpdate,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Update slot details (times or capacity)."""
    slot = session.get(ServiceSlot, slot_id)
    if (
        not slot
        or slot.service_center_id != current_center.id
        or slot.center_service_id != service_id
    ):
        raise HTTPException(status_code=404, detail="Slot not found")

    update_data = slot_update.model_dump(exclude_unset=True)

    # Check overlaps if time is changing
    new_start = update_data.get("start_time", slot.start_time)
    new_end = update_data.get("end_time", slot.end_time)

    if new_start >= new_end:
        raise HTTPException(
            status_code=400, detail="Start time must be before end time"
        )

    if "start_time" in update_data or "end_time" in update_data:
        overlap = session.exec(
            select(ServiceSlot).where(
                ServiceSlot.center_service_id == service_id,
                ServiceSlot.id != slot_id,
                ServiceSlot.start_time < new_end,
                ServiceSlot.end_time > new_start,
            )
        ).first()
        if overlap:
            raise HTTPException(
                status_code=400, detail="Updated times overlap with another slot."
            )

    if "capacity" in update_data and update_data["capacity"] < slot.booked_count:
        raise HTTPException(
            status_code=400,
            detail="Cannot reduce capacity below currently booked amount.",
        )

    for key, value in update_data.items():
        setattr(slot, key, value)

    session.add(slot)
    session.commit()
    session.refresh(slot)
    return ServiceSlotPublic(**slot.model_dump())


@router.delete("/me/services/{service_id}/slots/{slot_id}")
def delete_service_slot(
    service_id: int,
    slot_id: int,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Delete a service slot if it has no active bookings."""
    slot = session.get(ServiceSlot, slot_id)
    if (
        not slot
        or slot.service_center_id != current_center.id
        or slot.center_service_id != service_id
    ):
        raise HTTPException(status_code=404, detail="Slot not found")

    if slot.booked_count > 0:
        raise HTTPException(
            status_code=400, detail="Cannot delete a slot that has active bookings."
        )

    session.delete(slot)
    session.commit()
    return {"message": "Slot deleted successfully"}


@router.get("/me/services/{service_id}/slots", response_model=List[ServiceSlotPublic])
def get_my_service_slots(
    service_id: int,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Get all time slots for a specific service at this center.
    Functionality: List slots created by service center for a particular service
    """
    service = session.get(CenterService, service_id)
    if not service or service.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    slots = session.exec(
        select(ServiceSlot).where(ServiceSlot.center_service_id == service_id)
    ).all()
    return [ServiceSlotPublic(**s.model_dump()) for s in slots]


# --- BOOKING MANAGEMENT ---


@router.get("/me/bookings", response_model=List[ServiceRequestPublic])
def get_center_bookings(
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    status: str = Query(None),
    page: int = Query(1, gt=0),
    limit: int = Query(20, gt=0, le=100),
):
    """
    Get all service bookings for the current service center.
    Functionality: Service center views all customer bookings with optional status filter
    """
    offset = (page - 1) * limit
    query = (
        select(ServiceRequest)
        .where(ServiceRequest.service_center_id == current_center.id)
        .options(selectinload(ServiceRequest.user))
    )

    if status:
        query = query.where(ServiceRequest.status == status)

    query = query.order_by(desc(ServiceRequest.booking_time))
    bookings = session.exec(query.offset(offset).limit(limit)).all()

    result = []
    for b in bookings:
        b_dict = b.model_dump()
        b_dict["customer_name"] = b.user.full_name if b.user else "Unknown"
        b_dict["customer_phone"] = b.user.phone_number if b.user else "Unknown"
        result.append(ServiceRequestForCenter(**b_dict))

    return result


@router.get("/me/active-bookings", response_model=List[ServiceRequestPublic])
def get_center_active_bookings(
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Center-app polling target: only the center's currently-engaged bookings
    (booked → in_service). Short-TTL cached; the response model carries no
    customer phone/address."""
    key = cache.active_key("service_center", current_center.id)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached

    rows = session.exec(
        select(ServiceRequest)
        .where(
            ServiceRequest.service_center_id == current_center.id,
            ServiceRequest.status.in_(SERVICE_CENTER_ENGAGED_STATES),
        )
        .order_by(desc(ServiceRequest.booking_time))
    ).all()
    result = [
        ServiceRequestPublic.model_validate(s, from_attributes=True).model_dump(
            mode="json"
        )
        for s in rows
    ]
    cache.cache_set_json(key, result, cache.ACTIVE_CACHE_TTL)
    return result


@router.get("/me/bookings/{booking_id}/summary")
def get_center_booking_summary(
    booking_id: str,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Rich status summary for the center app's booking screen. Visible only to
    the center serving the booking. Returns a sanitized customer block (name +
    avatar, no phone), an actions block, and the amount to collect."""
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    return build_service_summary(session, booking, viewer="center")


@router.patch("/me/bookings/{booking_id}/status")
def update_booking_status(
    booking_id: str,
    new_status: str = Body(..., embed=True),
    cancellation_reason: str = Body(None, embed=True),
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Update the status of a service booking.
    Functionality: Service center updates booking status (searching->booked->completed, etc.)
    For cancellation: Provide cancellation_reason that will be visible to the user
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Validate status transitions
    valid_statuses = [e.value for e in ServiceStatus]
    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}",
        )

    previous_status = booking.status
    booking.status = new_status

    center_cancelled = False
    # Set completion time if status is completed
    if new_status == "completed":
        booking.completed_time = datetime.utcnow()
        booking.actual_return_date = date.today()
        booking.actual_return_time = datetime.utcnow().strftime("%H:%M")

    elif new_status == "cancelled":
        booking.cancellation_time = datetime.utcnow()
        # Store cancellation reason if provided
        if cancellation_reason:
            booking.cancellation_reason = cancellation_reason
        center_cancelled = True

        # Delete slot if it exists (for slot-based bookings) — frees capacity.
        if booking.slot_id:
            try:
                slot = session.get(ServiceSlot, booking.slot_id)
                if slot:
                    session.delete(slot)
            except NoResultFound:
                pass

    session.add(booking)
    session.commit()
    session.refresh(booking)

    if center_cancelled:
        refund_booking_payments(
            session,
            "service_center",
            booking.reference_id,
            reason=cancellation_reason or "Cancelled by service center",
            actor="service_center",
            actor_id=str(current_center.id),
        )

    # Keep the assigned member's active-assignment cache fresh on any transition,
    # and auto-assign when the booking becomes active and is still unassigned.
    if booking.assigned_member_id:
        member_assignment.invalidate_member_active_cache(booking.assigned_member_id)
    elif new_status in (
        ServiceStatus.ACCEPTED.value,
        ServiceStatus.CHECKED_IN.value,
        ServiceStatus.SERVICE_ONGOING.value,
    ):
        member_assignment.auto_assign_member(session, booking)

    return {
        "message": f"Booking status updated to {new_status}",
        "booking_id": booking_id,
        "previous_status": previous_status,
    }


@router.post("/me/bookings/{booking_id}/checkin")
def checkin_vehicle(
    booking_id: str,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Mark a vehicle as checked in for breakdown & repair service.
    Functionality: Service center checks in vehicle for manual breakdown & repair services
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    service = session.get(CenterService, booking.center_service_id)
    if service.booking_type != BookingType.WALK_IN:
        raise HTTPException(
            status_code=400, detail="Check-in is only for walk-in services"
        )

    if booking.status != ServiceStatus.ACCEPTED.value:
        raise HTTPException(
            status_code=400,
            detail="Vehicle can only be checked in from 'accepted' status",
        )

    booking.status = ServiceStatus.CHECKED_IN.value
    booking.checked_in_time = datetime.utcnow()

    session.add(booking)
    session.commit()
    session.refresh(booking)

    # Walk-in vehicle has arrived: if the center hasn't assigned a member yet,
    # auto-assign an available expert now.
    if not booking.assigned_member_id:
        member_assignment.auto_assign_member(session, booking)

    return {"message": "Vehicle checked in successfully"}


@router.patch("/me/bookings/{booking_id}/return-date")
def set_expected_return_date(
    booking_id: str,
    expected_return_date: date = Query(...),
    expected_return_time: str = Query(...),
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Set expected return date and time for breakdown & repair service.
    Functionality: Service center manually sets expected return date/time after vehicle inspection
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    service = session.get(CenterService, booking.center_service_id)
    if service.booking_type != BookingType.WALK_IN:
        raise HTTPException(
            status_code=400,
            detail="Expected return date is only for walk-in services",
        )

    if booking.status != "checked_in":
        raise HTTPException(
            status_code=400,
            detail="Expected return can only be set after vehicle check-in",
        )

    # Validate time format HH:MM
    try:
        datetime.strptime(expected_return_time, "%H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="Time format must be HH:MM")

    booking.expected_return_date = expected_return_date
    booking.expected_return_time = expected_return_time

    session.add(booking)
    session.commit()

    return {
        "message": "Expected return date set successfully",
        "expected_return_date": expected_return_date,
        "expected_return_time": expected_return_time,
        "status": booking.status,
    }


# --- WALK-IN SERVICE MANAGEMENT ---


@router.patch("/me/bookings/{booking_id}/walk-in-price-duration")
def update_walkin_price_and_duration(
    booking_id: str,
    expected_price: float = Query(..., gt=0),
    expected_return_datetime: datetime = Query(...),
    price_components: List[Dict[str, Any]] = Body(default=[]),
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Update price and expected return datetime for walk-in services.
    Only allowed when booking is in 'accepted' or 'service_ongoing' status.
    After 'service_accepted' status, price is locked and only datetime can be changed.
    Functionality: Service center updates walk-in service price and expected return after inspection
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    service = session.get(CenterService, booking.center_service_id)
    if service.booking_type != BookingType.WALK_IN:
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for walk-in services",
        )

    # Check if price can be modified
    if booking.price_locked:
        raise HTTPException(
            status_code=400,
            detail="Price is locked. Cannot modify after service acceptance",
        )

    # Check booking status
    if booking.status not in [
        ServiceStatus.CHECKED_IN.value,
        ServiceStatus.SERVICE_ONGOING.value,
    ]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot update price for booking in '{booking.status}' status",
        )

    # Validate and set price components
    if price_components:
        total_price = 0.0
        for component in price_components:
            if "amount" not in component or component["amount"] < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Each pricing component must have a valid amount",
                )
            total_price += component["amount"]
        booking.price_components = price_components
        booking.final_price = total_price
    else:
        booking.final_price = expected_price

    # If price is not set, update it
    if booking.price_at_booking is None:
        booking.price_at_booking = expected_price

    # Set expected return datetime
    booking.expected_return_date = expected_return_datetime.date()
    booking.expected_return_time = expected_return_datetime.strftime("%H:%M")

    if booking.status == ServiceStatus.CHECKED_IN:
        booking.status = ServiceStatus.SERVICE_ONGOING

    session.add(booking)
    session.commit()

    return {
        "message": "Walk-in service price and duration updated successfully",
        "booking_id": booking.reference_id,
        "final_price": booking.final_price,
        "price_components": booking.price_components,
        "expected_return_date": booking.expected_return_date,
        "expected_return_time": booking.expected_return_time,
        "status": booking.status,
    }


@router.patch("/me/bookings/{booking_id}/accept-service")
def accept_walkin_service(
    booking_id: str,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Accept walk-in service after price and duration are set.
    Transitions status from 'service_ongoing' to 'service_accepted'.
    After this, price is locked but expected_return_datetime can still be changed.
    Functionality: Service center confirms walk-in service details and locks price
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    service = session.get(CenterService, booking.center_service_id)
    if service.booking_type != BookingType.WALK_IN:
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for walk-in services",
        )

    if booking.status != ServiceStatus.SERVICE_ONGOING.value:
        raise HTTPException(
            status_code=400,
            detail=f"Service can only be accepted from 'service_ongoing' status, currently in '{booking.status}'",
        )

    # Check that price and return datetime are set
    if booking.final_price is None or booking.expected_return_date is None:
        raise HTTPException(
            status_code=400,
            detail="Price and expected return datetime must be set before acceptance",
        )

    # Lock the price and transition status
    booking.price_locked = True
    booking.status = ServiceStatus.SERVICE_ACCEPTED
    booking.service_accepted_time = datetime.utcnow()

    session.add(booking)
    session.commit()

    return {
        "message": "Walk-in service accepted and price locked",
        "booking_id": booking.reference_id,
        "status": booking.status,
        "final_price": booking.final_price,
        "price_locked": booking.price_locked,
        "service_accepted_time": booking.service_accepted_time,
    }


# --- CENTER MEMBER MANAGEMENT ---


def _member_for_center(
    session: Session, m: CenterMember, *, detail: bool = False
) -> CenterMemberForCenter:
    """Build the center's sanitized view of a member. `detail=True` adds the
    completed/pending/hours stats (omitted from list rows for cost)."""
    data: Dict[str, Any] = {
        "id": m.reference_id,
        "name": m.name,
        "gender": m.gender,
        "profile_picture_url": m.profile_picture_url,
        "expert_in": m.expert_in,
        "is_online": m.is_online,
        "status": m.status,
        "rating": m.rating,
        "total_reviews": m.total_reviews,
        "current_assignments": member_assignment.member_current_assignment_blocks(
            session, m.id
        ),
    }
    if detail:
        data.update(member_assignment.member_stats(session, m.id))
    return CenterMemberForCenter(**data)


@router.get("/me/members", response_model=List[CenterMemberForCenter])
def list_center_members(
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    status: str = Query(None, description="pending_approval|approved|rejected|suspended|banned"),
    expert: str = Query(None, description="filter to members with this service in expert_in"),
    page: int = Query(1, gt=0),
    limit: int = Query(50, gt=0, le=100),
):
    """List this center's members (sanitized — no internal id / center_code /
    phone). Filter by status (e.g. pending_approval to triage requests) or by an
    expertise/service name."""
    offset = (page - 1) * limit
    query = select(CenterMember).where(
        CenterMember.service_center_id == current_center.id
    )
    if status:
        query = query.where(CenterMember.status == status)
    query = query.order_by(desc(CenterMember.created_at))
    if expert:
        # expert_in is a JSON list (DB-agnostic) — filter then page in Python so
        # the page isn't silently short. Member counts per center are small.
        matched = [
            m for m in session.exec(query).all() if expert in (m.expert_in or [])
        ]
        members = matched[offset : offset + limit]
    else:
        members = session.exec(query.offset(offset).limit(limit)).all()
    return [_member_for_center(session, m) for m in members]


@router.get("/me/members/available", response_model=List[CenterMemberAssignCandidate])
def list_available_members(
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
    booking_id: str = Query(None, description="rank/filter candidates for this booking's service"),
    include_non_experts: bool = Query(True, description="soft expertise: include non-matches"),
):
    """Members eligible for assignment (approved + online). When `booking_id` is
    given, expertise matches are flagged and listed first; non-experts are
    included by default (manual assignment is soft — center may override)."""
    members = member_assignment.approved_online_members(session, current_center.id)

    service_name = None
    if booking_id:
        booking = get_by_reference(session, ServiceRequest, booking_id)
        if booking and booking.service_center_id == current_center.id:
            service_name = booking.service_name

    rows: List[CenterMemberAssignCandidate] = []
    for m in members:
        match = (
            member_assignment.expertise_matches(m, service_name)
            if service_name
            else True
        )
        if service_name and not match and not include_non_experts:
            continue
        # Fetch the member's active assignments once; derive count + soonest-free.
        active = member_assignment.member_active_assignments(session, m.id)
        ests = [member_assignment.completion_estimate(session, b) for b in active]
        ests = [e for e in ests if e]
        rows.append(
            CenterMemberAssignCandidate(
                id=m.reference_id,
                name=m.name,
                expert_in=m.expert_in,
                is_online=m.is_online,
                active_task_count=len(active),
                soonest_free_at=(min(ests) if ests else None),
                expertise_match=match,
            )
        )
    # experts first, then least-loaded.
    rows.sort(key=lambda r: (not r.expertise_match, r.active_task_count))
    return rows


@router.get("/me/members/{member_ref}", response_model=CenterMemberForCenter)
def get_center_member_detail(
    member_ref: str,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Full (sanitized) member detail + work stats (completed / pending / total
    hours worked). No internal id / center_code."""
    member = get_by_reference(session, CenterMember, member_ref)
    if not member or member.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Member not found")
    return _member_for_center(session, member, detail=True)


_APPROVAL_ACTIONS = {
    "approve": "approved",
    "reject": "rejected",
    "suspend": "suspended",
    "reactivate": "approved",
}


@router.patch("/me/members/{member_ref}/approval")
def update_member_approval(
    member_ref: str,
    body: MemberApprovalRequest,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Approve / reject / suspend / reactivate a member (the center owns this —
    no admin approval). Notifies the member."""
    member = get_by_reference(session, CenterMember, member_ref)
    if not member or member.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Member not found")

    action = (body.action or "").strip().lower()
    if action not in _APPROVAL_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="action must be one of: approve, reject, suspend, reactivate",
        )

    member.status = _APPROVAL_ACTIONS[action]
    session.add(member)
    session.commit()
    session.refresh(member)

    cache.cache_delete(cache.me_key("center_member", member.id))
    member_assignment.invalidate_member_active_cache(member.id)

    notify_safe(
        session=session,
        user_ids=[member.user_id],
        title="Membership update",
        body=f"Your membership status is now '{member.status}'."
        + (f" Note: {body.note}" if body.note else ""),
        data={"type": "center_member_status", "status": member.status},
    )
    return {
        "message": f"Member {action} done",
        "member_id": member.reference_id,
        "status": member.status,
    }


# --- BOOKING ⇄ MEMBER ASSIGNMENT ---


@router.patch("/me/bookings/{booking_id}/assign")
def assign_member_to_booking(
    booking_id: str,
    body: MemberAssignmentRequest,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Manually assign (or reassign) a member to a slot or walk-in booking.
    Approved + online members only; walk-in requires the vehicle to have checked
    in. Expertise is soft here (center may override)."""
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status in (
        ServiceStatus.COMPLETED.value,
        ServiceStatus.CANCELLED.value,
    ):
        raise HTTPException(
            status_code=400, detail="Cannot assign on a completed/cancelled booking"
        )

    member = get_by_reference(session, CenterMember, body.member_id)
    if not member or member.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.status != "approved":
        raise HTTPException(status_code=400, detail="Member is not approved")
    if not member.is_online:
        raise HTTPException(status_code=400, detail="Member is offline / unavailable")

    service = session.get(CenterService, booking.center_service_id)
    if service and service.booking_type == BookingType.WALK_IN and booking.status in (
        ServiceStatus.BOOKED.value,
        ServiceStatus.ACCEPTED.value,
    ):
        raise HTTPException(
            status_code=400,
            detail="Assign a member only after the vehicle has checked in",
        )

    previous = booking.assigned_member_id
    booking.assigned_member_id = member.id
    booking.assigned_at = datetime.utcnow()
    booking.auto_assigned = False
    session.add(booking)
    session.commit()
    session.refresh(booking)

    member_assignment.invalidate_member_active_cache(member.id)
    if previous and previous != member.id:
        member_assignment.invalidate_member_active_cache(previous)
    member_assignment.invalidate_center_active_cache(current_center.id)

    notify_safe(
        session=session,
        user_ids=[member.user_id],
        title="New assignment",
        body=f"You have been assigned to {booking.service_name} "
        f"(booking {booking.reference_id}).",
        data={
            "type": "service_assignment",
            "booking_id": booking.reference_id,
            "auto": False,
        },
    )
    return {
        "message": "Member assigned",
        "booking_id": booking.reference_id,
        "member_id": member.reference_id,
    }


@router.delete("/me/bookings/{booking_id}/assign")
def unassign_member_from_booking(
    booking_id: str,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """Clear the member assignment on a booking."""
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    previous = booking.assigned_member_id
    booking.assigned_member_id = None
    booking.assigned_at = None
    booking.auto_assigned = False
    session.add(booking)
    session.commit()

    if previous:
        member_assignment.invalidate_member_active_cache(previous)
    member_assignment.invalidate_center_active_cache(current_center.id)
    return {"message": "Member unassigned", "booking_id": booking_id}
