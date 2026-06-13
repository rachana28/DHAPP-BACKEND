from fastapi import APIRouter, Depends, HTTPException, Query, Body, BackgroundTasks
from sqlmodel import Session, select, desc, func
from typing import List, Dict, Any
from datetime import datetime, date, timedelta, time
from sqlalchemy.exc import NoResultFound
from sqlalchemy import cast, String
from app.core.database import get_session
from app.core.models import (
    ServiceCenter,
    ServiceCenterPublic,
    CenterService,
    CenterServicePublic,
    ServiceSlot,
    ServiceRequest,
    ServiceRequestCreate,
    ServiceRequestPublic,
    ServiceCenterReview,
    CenterMember,
    CenterMemberReview,
    User,
    BookingType,
    ServiceStatus,
    SystemConfig,
)
from app.core import cache
from app.modules.bookings.ratings import RatingIn, record_service_rating
from app.modules.payments.service import refund_booking_payments
from app.modules.service.booking_summary import build_service_summary
from app.services.dues import raise_if_unpaid_past_due
from app.core.security import get_current_user
from app.utils.notifications import send_push_notification
from app.utils.id_generator import (
    generate_reference_id,
    get_by_reference,
    SERVICE_REQUEST,
)
import math

router = APIRouter(prefix="/services", tags=["User Services"])


SERVICE_CENTER_ADVANCE_PCT_KEY = "service_center_advance_pct"
DEFAULT_SERVICE_CENTER_ADVANCE_PCT = 0.20

SERVICE_CENTER_LATE_CANCEL_HOURS_KEY = "service_center_late_cancel_hours"
DEFAULT_SERVICE_CENTER_LATE_CANCEL_HOURS = 6.0


def _service_center_advance_pct(session: Session) -> float:
    cfg = session.get(SystemConfig, SERVICE_CENTER_ADVANCE_PCT_KEY)
    if cfg and cfg.value:
        try:
            v = float(cfg.value)
            if 0.0 <= v <= 1.0:
                return v
        except (TypeError, ValueError):
            pass
    return DEFAULT_SERVICE_CENTER_ADVANCE_PCT


def _service_center_late_cancel_hours(session: Session) -> float:
    cfg = session.get(SystemConfig, SERVICE_CENTER_LATE_CANCEL_HOURS_KEY)
    if cfg and cfg.value:
        try:
            return float(cfg.value)
        except (TypeError, ValueError):
            pass
    return DEFAULT_SERVICE_CENTER_LATE_CANCEL_HOURS


# --- SERVICE CENTER DISCOVERY ---


@router.get("/center-lookup")
def lookup_center_by_code(
    code: str = Query(..., min_length=6, max_length=6),
    session: Session = Depends(get_session),
):
    """Public helper for the driver-app center-member signup screen: resolve a
    6-char center_code to the center's name and the list of service names that
    populate the mandatory `expert_in` dropdown. No auth (pre-registration)."""
    center = session.exec(
        select(ServiceCenter).where(ServiceCenter.center_code == code.strip().upper())
    ).first()
    if not center:
        raise HTTPException(status_code=404, detail="Invalid center code")
    return {
        "center_id": center.reference_id,
        "center_name": center.name,
        "status": center.status,
        "service_names": sorted({s.service_name for s in center.services}),
    }


@router.get("/centers", response_model=List[ServiceCenterPublic])
def list_service_centers(
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(20, gt=0, le=100),
    latitude: float = Query(None),
    longitude: float = Query(None),
    service_name: str = Query(None),
    vehicle_type: str = Query(None),
):
    """
    List all available service centers, sorted by distance if coordinates are provided.
    """
    offset = (page - 1) * limit

    # Base query: only approved centers
    query = select(ServiceCenter).where(ServiceCenter.status == "available")

    if service_name or vehicle_type:
        query = query.join(CenterService)

        if service_name:
            query = query.where(CenterService.service_name.ilike(f"%{service_name}%"))

        if vehicle_type:
            query = query.where(
                cast(CenterService.vehicle_types, String).ilike(f'%"{vehicle_type}"%')
            )

    # Standard fallback sort by rating
    query = query.order_by(desc(ServiceCenter.rating))
    raw_centers = session.exec(query).all()

    unique_centers_map = {}
    for center in raw_centers:
        if center.id not in unique_centers_map:
            unique_centers_map[center.id] = center

    # Convert back to list and apply pagination
    centers = list(unique_centers_map.values())[offset : offset + limit]

    # Helper function to calculate distance in km
    def calculate_distance(lat1, lon1, lat2, lon2):
        if None in (lat1, lon1, lat2, lon2):
            return None
        R = 6371.0  # Earth radius in kilometers
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(R * c, 1)

    result = []
    for center in centers:
        booking_count = session.exec(
            select(func.count(ServiceRequest.id)).where(
                ServiceRequest.service_center_id == center.id
            )
        ).one()

        # Calculate distance
        dist = calculate_distance(
            latitude, longitude, center.latitude, center.longitude
        )

        result.append(
            ServiceCenterPublic(
                **center.model_dump(), total_bookings=booking_count, distance=dist
            )
        )

    # If the mobile app provided coordinates, overwrite the rating sort with a distance sort
    if latitude and longitude:
        result.sort(key=lambda x: x.distance if x.distance is not None else 9999.0)

    return result


@router.get("/centers/{center_id}", response_model=ServiceCenterPublic)
def get_service_center_details(
    center_id: str,
    session: Session = Depends(get_session),
):
    """
    Get detailed information about a specific service center.
    """
    center = get_by_reference(session, ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    if center.status not in ["available", "pending_approval"]:
        raise HTTPException(status_code=403, detail="Service center not available")

    booking_count = session.exec(
        select(func.count(ServiceRequest.id)).where(
            ServiceRequest.service_center_id == center.id
        )
    ).one()

    return ServiceCenterPublic(**center.model_dump(), total_bookings=booking_count)


# --- SERVICE BROWSING ---


@router.get("/centers/{center_id}/services", response_model=List[CenterServicePublic])
def list_center_services(
    center_id: str,
    session: Session = Depends(get_session),
    vehicle_type: str = Query(None),
):
    """
    Get all services offered by a specific service center.
    Optionally filter by vehicle type.
    """
    center = get_by_reference(session, ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    query = select(CenterService).where(CenterService.service_center_id == center.id)

    # Filter by vehicle type if provided
    if vehicle_type:
        services = session.exec(query).all()
        services = [
            s
            for s in services
            if vehicle_type in s.vehicle_types or not s.vehicle_types
        ]
        return [CenterServicePublic(**s.model_dump()) for s in services]

    services = session.exec(query).all()
    return [CenterServicePublic(**s.model_dump()) for s in services]


@router.get(
    "/centers/{center_id}/services/{service_id}/available-times",
    response_model=List[Dict[str, Any]],
)
def get_available_times(
    center_id: str,
    service_id: int,
    target_date: date = Query(...),
    session: Session = Depends(get_session),
):
    """
    Dynamically generates available start times for a given day based on service rules
    and existing bookings.
    """
    center = get_by_reference(session, ServiceCenter, center_id)
    service = session.get(CenterService, service_id)
    if not center or not service or service.service_center_id != center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    if service.booking_type == BookingType.WALK_IN:
        return [{"message": "Walk-in service. No specific slots required."}]

    if not service.slot_start_time or not service.slot_end_time:
        raise HTTPException(
            status_code=400, detail="Provider has not configured operating hours."
        )

    # 1. Check Max Bookings Per Day
    start_of_day = datetime.combine(target_date, time.min)
    end_of_day = datetime.combine(target_date, time.max)

    daily_bookings_count = session.exec(
        select(func.count(ServiceSlot.id)).where(
            ServiceSlot.center_service_id == service.id,
            ServiceSlot.start_time >= start_of_day,
            ServiceSlot.start_time <= end_of_day,
        )
    ).one()

    if daily_bookings_count >= service.max_daily_bookings:
        return []  # Fully booked for the day

    # 2. Generate Potential Start Times
    available_times = []

    # FIX: Parse string times to datetime.time objects
    start_t = datetime.strptime(service.slot_start_time, "%H:%M").time()
    end_t = datetime.strptime(service.slot_end_time, "%H:%M").time()

    current_time = datetime.combine(target_date, start_t)
    end_operating_time = datetime.combine(target_date, end_t)

    # Convert duration to timedelta
    duration_delta = timedelta(hours=service.service_duration_hours)

    while current_time < end_operating_time:
        potential_end_time = current_time + duration_delta

        # 3. Check Overlaps
        overlap_count = session.exec(
            select(func.count(ServiceSlot.id)).where(
                ServiceSlot.center_service_id == service.id,
                ServiceSlot.start_time < potential_end_time,
                ServiceSlot.end_time > current_time,
            )
        ).one()

        if overlap_count < service.max_concurrent_bookings:
            available_times.append(
                {
                    "start_time": current_time.isoformat(),
                    "end_time": potential_end_time.isoformat(),
                }
            )

        current_time += timedelta(minutes=service.slot_interval_minutes)

    return available_times


@router.get("/centers/{center_id}/services/{service_id}/walk-in-availability")
def check_walk_in_availability(
    center_id: str,
    service_id: int,
    session: Session = Depends(get_session),
):
    """
    Check availability for walk-in services.
    """
    center = get_by_reference(session, ServiceCenter, center_id)
    service = session.get(CenterService, service_id)
    if not center or not service or service.service_center_id != center.id:
        raise HTTPException(status_code=404, detail="Service not found")

    if service.booking_type != BookingType.WALK_IN or not service.is_walk_in_allowed:
        raise HTTPException(
            status_code=400, detail="This service is not available for walk-in bookings"
        )

    return {
        "service_id": service.id,
        "service_name": service.service_name,
        "booking_type": service.booking_type.value,
        "is_available": service.is_available,
        "is_walk_in_allowed": service.is_walk_in_allowed,
        "message": "Available for walk-in"
        if service.is_available
        else "Currently unavailable",
    }


# --- BOOKING ---


@router.post("/book", response_model=ServiceRequestPublic)
def book_service(
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    booking_data: ServiceRequestCreate,
    background_tasks: BackgroundTasks,
):
    """
    Book a service at a service center.
    Auto-accepts booking based on availability and configured rules.
    """
    if current_user.role != "user":
        raise HTTPException(status_code=403, detail="Only users can book services")

    raise_if_unpaid_past_due(session, current_user.id)

    # Verify service center exists and is available
    center = get_by_reference(session, ServiceCenter, booking_data.service_center_id)
    if not center or center.status != "available":
        raise HTTPException(status_code=404, detail="Service center not available")

    # Verify service exists at center
    service = session.get(CenterService, booking_data.center_service_id)
    if not service or service.service_center_id != center.id:
        raise HTTPException(status_code=404, detail="Service not found at this center")

    # Verify vehicle type is in allowed types
    if booking_data.vehicle_type not in service.vehicle_types:
        raise HTTPException(
            status_code=400,
            detail=f"Vehicle type '{booking_data.vehicle_type}' not supported for this service",
        )

    # Verify booking_type matches service booking_type
    if booking_data.booking_type != service.booking_type:
        raise HTTPException(
            status_code=400,
            detail=f"Booking type mismatch. Service is {service.booking_type.value}",
        )

    expected_return_date = None
    expected_return_time = None
    price_at_booking = None
    price_components = []
    slot_id = None
    advance_amount = None

    # --- SLOT-BASED BOOKING ---
    if service.booking_type == BookingType.SLOT_BASED:
        if not booking_data.requested_date or not booking_data.requested_time:
            raise HTTPException(
                status_code=400,
                detail="requested_date and requested_time are required for slot-based services.",
            )

        try:
            req_time_obj = datetime.strptime(
                booking_data.requested_time, "%H:%M"
            ).time()
            start_time = datetime.combine(booking_data.requested_date, req_time_obj)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid time format. Use HH:MM"
            )

        if start_time < datetime.utcnow():
            raise HTTPException(
                status_code=400,
                detail="Cannot book a time slot in the past. Please select a future time.",
            )

        end_time = start_time + timedelta(hours=service.service_duration_hours)

        session.exec(
            select(CenterService)
            .where(CenterService.id == service.id)
            .with_for_update()
        ).first()

        # Check overlaps (now under the lock, so the count is consistent).
        overlap_count = session.exec(
            select(func.count(ServiceSlot.id)).where(
                ServiceSlot.center_service_id == service.id,
                ServiceSlot.start_time < end_time,
                ServiceSlot.end_time > start_time,
            )
        ).one()

        # At/over capacity no longer hard-rejects (D10). The booking is held in
        # `pending_confirmation` for the center to accept or decline; within
        # capacity it auto-confirms to `booked` as before.
        over_capacity = overlap_count >= service.max_concurrent_bookings

        # Create the physical locked slot
        locked_slot = ServiceSlot(
            service_center_id=service.service_center_id,
            center_service_id=service.id,
            start_time=start_time,
            end_time=end_time,
        )
        session.add(locked_slot)
        session.flush()  # Get the slot ID

        slot_id = locked_slot.id

        # INTELLIGENT EXPECTED RETURN (Rollover check)
        end_t = datetime.strptime(service.slot_end_time, "%H:%M").time()
        operating_end = datetime.combine(start_time.date(), end_t)

        if end_time > operating_end:
            overtime = end_time - operating_end
            next_day = start_time.date() + timedelta(days=1)

            start_t = datetime.strptime(service.slot_start_time, "%H:%M").time()
            next_day_start = datetime.combine(next_day, start_t)

            expected_return_datetime = next_day_start + overtime
        else:
            expected_return_datetime = end_time

        expected_return_date = expected_return_datetime.date()
        expected_return_time = expected_return_datetime.strftime("%H:%M")

        price_at_booking = service.price
        price_components = service.pricing_components or []
        advance_pct = _service_center_advance_pct(session)
        advance_amount = (
            round((service.price or 0.0) * advance_pct, 2) if service.price else None
        )
        booking_status = (
            ServiceStatus.PENDING_CONFIRMATION
            if over_capacity
            else ServiceStatus.BOOKED
        )

    # --- WALK-IN BOOKING ---
    elif service.booking_type == BookingType.WALK_IN:
        if not service.is_walk_in_allowed or not service.is_available:
            raise HTTPException(
                status_code=400, detail="Walk-in service is not available"
            )

        # For walk-in: price and return are initially blank (filled after inspection)
        price_at_booking = None
        price_components = []
        booking_status = ServiceStatus.ACCEPTED

    else:
        raise HTTPException(
            status_code=400, detail=f"Unknown booking type: {service.booking_type}"
        )

    # Create booking with auto-accept status
    new_booking = ServiceRequest(
        user_id=current_user.id,
        reference_id=generate_reference_id(session, SERVICE_REQUEST),
        service_center_id=center.id,
        center_service_id=booking_data.center_service_id,
        booking_type=booking_data.booking_type,
        service_name=service.service_name,
        vehicle_type=booking_data.vehicle_type,
        vehicle_number=booking_data.vehicle_number,
        vehicle_model=booking_data.vehicle_model,
        slot_id=slot_id,
        status=booking_status,
        booking_time=datetime.utcnow(),
        requested_date=booking_data.requested_date,
        requested_time=booking_data.requested_time,
        expected_return_date=expected_return_date,
        expected_return_time=expected_return_time,
        price_at_booking=price_at_booking,
        final_price=price_at_booking,
        price_locked=service.booking_type == BookingType.SLOT_BASED,
        price_components=price_components,
        advance_amount=advance_amount,
        amount_paid=0.0,
    )

    session.add(new_booking)
    session.commit()
    session.refresh(new_booking)

    # Notify service center about new booking
    booking_type_label = service.booking_type.value.replace("_", "-").title()
    background_tasks.add_task(
        send_push_notification,
        session=session,
        user_ids=[center.user_id],
        title="New Booking 📅",
        body=f"New {booking_type_label} booking for {service.service_name}",
        data={"booking_id": new_booking.reference_id, "type": "new_booking"},
    )

    return ServiceRequestPublic(**new_booking.model_dump())


# --- MY BOOKINGS ---


@router.get("/my-bookings", response_model=List[ServiceRequestPublic])
def get_my_service_bookings(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    page: int = Query(1, gt=0),
    limit: int = Query(20, gt=0, le=100),
    status: str = Query(None),
):
    """
    Get all service bookings for the current user.
    """
    if current_user.role != "user":
        raise HTTPException(
            status_code=403, detail="Only users can view their bookings"
        )

    offset = (page - 1) * limit
    query = select(ServiceRequest).where(ServiceRequest.user_id == current_user.id)

    if status:
        query = query.where(ServiceRequest.status == status)

    query = query.order_by(desc(ServiceRequest.booking_time))
    bookings = session.exec(query.offset(offset).limit(limit)).all()

    return [ServiceRequestPublic(**b.model_dump()) for b in bookings]


@router.get("/my-bookings/{booking_id}/summary")
def get_my_service_booking_summary(
    booking_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Rich status summary for the user app's service-booking screen. Visible to
    the booking's owner only. No phone numbers / sensitive data are returned."""
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    return build_service_summary(session, booking, viewer="user")


@router.patch("/my-bookings/{booking_id}/cancel")
def cancel_service_booking(
    booking_id: str,
    cancellation_reason: str = Body(None, embed=True),
    *,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Cancel a service booking and free up the slot.
    User can optionally provide cancellation reason for reference.
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status in [ServiceStatus.COMPLETED.value, ServiceStatus.CANCELLED.value]:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel a completed or already cancelled booking",
        )

    slot_start = None
    if booking.slot_id:
        try:
            slot = session.get(ServiceSlot, booking.slot_id)
            if slot:
                slot_start = slot.start_time
                session.delete(slot)
        except NoResultFound:
            pass

    late_cancel = False
    if slot_start is not None:
        cutoff = slot_start - timedelta(
            hours=_service_center_late_cancel_hours(session)
        )
        late_cancel = datetime.utcnow() >= cutoff

    booking.status = ServiceStatus.CANCELLED.value
    booking.cancellation_time = datetime.utcnow()
    # Store user's cancellation reason if provided
    if cancellation_reason:
        booking.cancellation_reason = cancellation_reason
    if late_cancel:
        booking.cancellation_reason = (
            (cancellation_reason + " " if cancellation_reason else "")
            + "(advance forfeited — cancelled within the late-cancellation window)"
        ).strip()

    session.add(booking)
    session.commit()

    refunded = []
    if not late_cancel:
        refunded = refund_booking_payments(
            session,
            "service_center",
            booking.reference_id,
            reason=cancellation_reason or "Booking cancelled by user",
            actor="user",
            actor_id=str(current_user.id),
        )

    # Notify service center
    center = session.get(ServiceCenter, booking.service_center_id)
    if center:
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[center.user_id],
            title="Booking Cancelled ❌",
            body="A customer cancelled their service booking",
            data={"booking_id": booking.reference_id, "type": "cancellation"},
        )

    return {
        "message": "Booking cancelled",
        "booking_id": booking.reference_id,
        "advance_forfeited": late_cancel,
        "refunded_payments": refunded,
    }

    return {"message": "Booking cancelled successfully"}


@router.patch("/my-bookings/{booking_id}", response_model=ServiceRequestPublic)
def update_service_booking(
    booking_id: str,
    vehicle_number: str = Body(None, embed=True),
    vehicle_model: str = Body(None, embed=True),
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Update booking details (vehicle_number and vehicle_model).
    Functionality: User can edit vehicle details before service is completed.
    Only allows editing if booking hasn't been completed or cancelled.
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Allow editing only if booking is not completed or cancelled
    if booking.status in [ServiceStatus.COMPLETED.value, ServiceStatus.CANCELLED.value]:
        raise HTTPException(
            status_code=400,
            detail="Cannot edit a completed or cancelled booking",
        )

    # Update provided fields
    if vehicle_number is not None:
        booking.vehicle_number = vehicle_number
    if vehicle_model is not None:
        booking.vehicle_model = vehicle_model

    session.add(booking)
    session.commit()
    session.refresh(booking)

    return ServiceRequestPublic(**booking.model_dump())


# --- REVIEWS ---


@router.post("/my-bookings/{booking_id}/service-review")
def submit_app_service_review(
    booking_id: str,
    payload: RatingIn,
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Rate DriveHub's service for a completed service-center booking (the app
    rating — distinct from the center and the technician). Stored in the unified
    ``ServiceRating`` table."""
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != ServiceStatus.COMPLETED.value:
        raise HTTPException(
            status_code=400, detail="Can only review completed bookings"
        )
    return record_service_rating(
        session,
        service_type="service_center",
        booking_id=booking.id,
        booking_reference_id=booking.reference_id,
        user_id=current_user.id,
        payload=payload,
    )


@router.post("/my-bookings/{booking_id}/review")
def submit_service_review(
    booking_id: str,
    payload: RatingIn,
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Submit a review of the service center for a completed booking.
    """
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status != ServiceStatus.COMPLETED.value:
        raise HTTPException(
            status_code=400, detail="Can only review completed bookings"
        )

    # Check if review already exists
    existing_review = session.exec(
        select(ServiceCenterReview).where(
            ServiceCenterReview.user_id == current_user.id,
            ServiceCenterReview.service_request_id == booking.id,
        )
    ).first()

    if existing_review:
        raise HTTPException(
            status_code=400, detail="You have already reviewed this booking"
        )

    # Create review
    review = ServiceCenterReview(
        service_center_id=booking.service_center_id,
        user_id=current_user.id,
        service_request_id=booking.id,
        rating=payload.rating,
        comment=payload.comment,
    )

    session.add(review)

    # Update service center average rating
    center = session.get(ServiceCenter, booking.service_center_id)
    all_reviews = session.exec(
        select(ServiceCenterReview).where(
            ServiceCenterReview.service_center_id == center.id
        )
    ).all()

    if all_reviews:
        avg_rating = sum(r.rating for r in all_reviews) / len(all_reviews)
        center.rating = round(avg_rating, 1)
        session.add(center)

    session.commit()

    return {
        "message": "Review submitted successfully",
        "rating": payload.rating,
        "center_new_rating": center.rating,
    }


@router.post("/my-bookings/{booking_id}/member-review")
def submit_member_review(
    booking_id: str,
    payload: RatingIn,
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Rate the technician/member who handled this booking. The member stays
    ANONYMOUS to the customer — they are derived from the booking's assignment.
    Called alongside the service-center review from the post-completion screen;
    no-ops gracefully if the booking was never assigned to a member."""
    booking = get_by_reference(session, ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != ServiceStatus.COMPLETED.value:
        raise HTTPException(
            status_code=400, detail="Can only review completed bookings"
        )
    if not booking.assigned_member_id:
        raise HTTPException(
            status_code=400, detail="No technician handled this booking"
        )

    existing_review = session.exec(
        select(CenterMemberReview).where(
            CenterMemberReview.user_id == current_user.id,
            CenterMemberReview.service_request_id == booking.id,
        )
    ).first()
    if existing_review:
        raise HTTPException(
            status_code=400, detail="You have already reviewed this booking's service"
        )

    review = CenterMemberReview(
        center_member_id=booking.assigned_member_id,
        user_id=current_user.id,
        service_request_id=booking.id,
        rating=payload.rating,
        comment=payload.comment,
    )
    session.add(review)

    # Recompute the member's aggregate (autoflush includes the new review).
    member = session.get(CenterMember, booking.assigned_member_id)
    all_reviews = session.exec(
        select(CenterMemberReview).where(
            CenterMemberReview.center_member_id == member.id
        )
    ).all()
    if all_reviews:
        member.rating = round(sum(r.rating for r in all_reviews) / len(all_reviews), 1)
        member.total_reviews = len(all_reviews)
        session.add(member)

    session.commit()
    cache.cache_delete(cache.me_key("center_member", member.id))
    return {"message": "Review submitted successfully", "rating": payload.rating}
