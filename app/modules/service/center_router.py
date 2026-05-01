"""
Service Center API Endpoints
Handles service center profile management, service offerings, slots, and booking management
"""

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlmodel import Session, select, func, desc
from typing import List
import redis
from datetime import datetime, date

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
)
from app.core.security import get_current_active_service_center
from app.utils.storage import upload_profile_picture_to_r2

router = APIRouter(prefix="/service-centers", tags=["Service Centers"])


# --- PROFILE MANAGEMENT ---


@router.get("/me", response_model=ServiceCenterPrivate)
def read_current_service_center_profile(
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Get the full profile for the currently authenticated service center.
    Functionality: Retrieve service center's own complete profile information
    """
    return current_center


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

    session.add(current_center)
    session.commit()
    session.refresh(current_center)

    # Invalidate cache for updated center
    if redis_client:
        redis_client.delete(f"service_center_{current_center.id}")

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
    public_url = await upload_profile_picture_to_r2(
        file, "kyc_documents", str(current_center.id)
    )
    
    # Append the new document URL to the JSON array safely
    current_docs = current_center.verification_documents or []
    
    # Create a new list to trigger SQLAlchemy's JSON mutation detection
    current_center.verification_documents = [*current_docs, public_url]
    
    session.add(current_center)
    session.commit()
    session.refresh(current_center)
    
    return {
        "message": "Document uploaded successfully", 
        "documents": current_center.verification_documents
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

    return current_center


@router.get("/{center_id}", response_model=ServiceCenterPublic)
def read_service_center(
    center_id: int,
    session: Session = Depends(get_session),
):
    """
    Get details for a specific service center by ID.
    Functionality: Public endpoint to view service center profile with total bookings
    """
    center = session.get(ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    booking_count = session.exec(
        select(func.count(ServiceRequest.id)).where(
            ServiceRequest.service_center_id == center_id
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
    Add a new service to the service center's offerings.
    Functionality: Service center can add services (breakdown_repair, ppf, wash, general_service) with duration and price
    """
    # Validate service type
    valid_types = ["breakdown_repair", "ppf", "wash", "general_service"]
    if service_data.service_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid service type. Must be one of: {', '.join(valid_types)}",
        )

    # Check if service already exists for this center
    existing = session.exec(
        select(CenterService).where(
            CenterService.service_center_id == current_center.id,
            CenterService.service_type == service_data.service_type,
        )
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Service center already offers {service_data.service_type}",
        )

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
    Update service details (duration, price, availability).
    Functionality: Modify expected duration, pricing, or availability of a service
    """
    service = session.get(CenterService, service_id)
    if not service or service.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    update_data = service_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(service, key, value)

    session.add(service)
    session.commit()
    session.refresh(service)

    return CenterServicePublic(**service.model_dump())


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
    Functionality: Service center creates time slots for PPF, wash, general service bookings
    """
    service = session.get(CenterService, service_id)
    if not service or service.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    # Slot-based services only
    if service.service_type == "breakdown_repair":
        raise HTTPException(
            status_code=400,
            detail="Slots are not applicable for breakdown & repair service",
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
    query = select(ServiceRequest).where(
        ServiceRequest.service_center_id == current_center.id
    )

    if status:
        query = query.where(ServiceRequest.status == status)

    query = query.order_by(desc(ServiceRequest.booking_time))
    bookings = session.exec(query.offset(offset).limit(limit)).all()

    return [ServiceRequestPublic(**b.model_dump()) for b in bookings]


@router.patch("/me/bookings/{booking_id}/status")
def update_booking_status(
    booking_id: int,
    new_status: str = Query(...),
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Update the status of a service booking.
    Functionality: Service center updates booking status (searching->booked->completed, etc.)
    """
    booking = session.get(ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Validate status transitions
    valid_statuses = [
        "searching",
        "booked",
        "checked_in",
        "in_service",
        "completed",
        "cancelled",
    ]
    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}",
        )

    booking.status = new_status

    # Set completion time if status is completed
    if new_status == "completed":
        booking.completed_time = datetime.utcnow()
        booking.actual_return_date = date.today()
        booking.actual_return_time = datetime.utcnow().strftime("%H:%M")

    # Free up slot capacity if center cancels
    elif new_status == "cancelled" and booking.slot_id:
        slot = session.get(ServiceSlot, booking.slot_id)
        if slot and slot.booked_count > 0:
            slot.booked_count -= 1
            session.add(slot)

    session.add(booking)
    session.commit()

    return {"message": f"Booking status updated to {new_status}"}


@router.post("/me/bookings/{booking_id}/checkin")
def checkin_vehicle(
    booking_id: int,
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Mark a vehicle as checked in for breakdown & repair service.
    Functionality: Service center checks in vehicle for manual breakdown & repair services
    """
    booking = session.get(ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    service = session.get(CenterService, booking.center_service_id)
    if service.service_type != "breakdown_repair":
        raise HTTPException(
            status_code=400, detail="Check-in is only for breakdown & repair services"
        )

    if booking.status != "booked":
        raise HTTPException(
            status_code=400,
            detail="Vehicle can only be checked in from 'booked' status",
        )

    booking.status = "checked_in"
    booking.checked_in_time = datetime.utcnow()

    session.add(booking)
    session.commit()

    return {"message": "Vehicle checked in successfully"}


@router.patch("/me/bookings/{booking_id}/return-date")
def set_expected_return_date(
    booking_id: int,
    expected_return_date: date = Query(...),
    expected_return_time: str = Query(...),  # Format: HH:MM
    *,
    session: Session = Depends(get_session),
    current_center: ServiceCenter = Depends(get_current_active_service_center),
):
    """
    Set expected return date and time for breakdown & repair service.
    Functionality: Service center manually sets expected return date/time after vehicle inspection
    """
    booking = session.get(ServiceRequest, booking_id)
    if not booking or booking.service_center_id != current_center.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    service = session.get(CenterService, booking.center_service_id)
    if service.service_type != "breakdown_repair":
        raise HTTPException(
            status_code=400,
            detail="Expected return date is only for breakdown & repair services",
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
    booking.status = "in_service"

    session.add(booking)
    session.commit()

    return {
        "message": "Expected return date set successfully",
        "expected_return_date": expected_return_date,
        "expected_return_time": expected_return_time,
    }
