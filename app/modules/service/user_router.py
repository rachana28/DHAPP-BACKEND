from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlmodel import Session, select, desc, func
from typing import List
from datetime import datetime, date, timedelta
from sqlalchemy.exc import NoResultFound
from app.core.database import get_session
from app.core.models import (
    ServiceCenter,
    ServiceCenterPublic,
    CenterService,
    CenterServicePublic,
    ServiceSlot,
    ServiceSlotPublic,
    ServiceRequest,
    ServiceRequestCreate,
    ServiceRequestPublic,
    ServiceCenterReview,
    User,
)
from app.core.security import get_current_user
from app.utils.notifications import send_push_notification

router = APIRouter(prefix="/services", tags=["User Services"])


# --- SERVICE CENTER DISCOVERY ---


@router.get("/centers", response_model=List[ServiceCenterPublic])
def list_service_centers(
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(20, gt=0, le=100),
    latitude: float = Query(None),
    longitude: float = Query(None),
    service_type: str = Query(None),  # Optional filter by service type
):
    """
    List all available service centers.
    Functionality: Retrieve list of approved service centers, optionally filtered by location/service type
    """
    offset = (page - 1) * limit

    # Base query: only approved centers
    query = select(ServiceCenter).where(ServiceCenter.status == "available")

    # Optional: filter by service type if provided
    if service_type:
        query = (
            query.distinct()
            .join(CenterService)
            .where(CenterService.service_type == service_type)
        )

    query = query.order_by(desc(ServiceCenter.rating))
    centers = session.exec(query.offset(offset).limit(limit)).all()

    result = []
    for center in centers:
        booking_count = session.exec(
            select(func.count(ServiceRequest.id)).where(
                ServiceRequest.service_center_id == center.id
            )
        ).one()
        result.append(
            ServiceCenterPublic(**center.model_dump(), total_bookings=booking_count)
        )

    return result


@router.get("/centers/{center_id}", response_model=ServiceCenterPublic)
def get_service_center_details(
    center_id: int,
    session: Session = Depends(get_session),
):
    """
    Get detailed information about a specific service center.
    Functionality: View service center profile with location and rating
    """
    center = session.get(ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    if center.status not in ["available", "pending_approval"]:
        raise HTTPException(status_code=403, detail="Service center not available")

    booking_count = session.exec(
        select(func.count(ServiceRequest.id)).where(
            ServiceRequest.service_center_id == center_id
        )
    ).one()

    return ServiceCenterPublic(**center.model_dump(), total_bookings=booking_count)


# --- SERVICE BROWSING ---


@router.get("/centers/{center_id}/services", response_model=List[CenterServicePublic])
def list_center_services(
    center_id: int,
    session: Session = Depends(get_session),
):
    """
    Get all services offered by a specific service center.
    Functionality: View service types and details available at a particular center
    """
    center = session.get(ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    services = session.exec(
        select(CenterService).where(CenterService.service_center_id == center_id)
    ).all()

    return [CenterServicePublic(**s.model_dump()) for s in services]


@router.get(
    "/centers/{center_id}/services/{service_type}/slots",
    response_model=List[ServiceSlotPublic],
)
def get_available_slots(
    center_id: int,
    service_type: str,
    session: Session = Depends(get_session),
    from_date: date = Query(None),  # Start date range
    to_date: date = Query(None),  # End date range
):
    """
    Get available time slots for a specific service at a service center.
    Functionality: List available slots for slot-based services (PPF, wash, general service)
    """
    # Verify service exists and is not breakdown_repair (no slots for that)
    service = session.exec(
        select(CenterService).where(
            CenterService.service_center_id == center_id,
            CenterService.service_type == service_type,
        )
    ).first()

    if not service:
        raise HTTPException(status_code=404, detail="Service not found at this center")

    if service.service_type == "breakdown_repair":
        raise HTTPException(
            status_code=400, detail="Breakdown & repair service doesn't use time slots"
        )

    # Query slots
    query = select(ServiceSlot).where(
        ServiceSlot.center_service_id == service.id,
        ServiceSlot.is_available,
        ServiceSlot.booked_count < ServiceSlot.capacity,  # Available capacity
    )

    # Filter by date range if provided
    if from_date:
        query = query.where(
            ServiceSlot.start_time >= datetime.combine(from_date, datetime.min.time())
        )
    if to_date:
        query = query.where(
            ServiceSlot.end_time <= datetime.combine(to_date, datetime.max.time())
        )

    slots = session.exec(query.order_by(ServiceSlot.start_time)).all()

    return [ServiceSlotPublic(**s.model_dump()) for s in slots]


@router.get("/centers/{center_id}/services/{service_type}/availability")
def check_breakdown_availability(
    center_id: int,
    service_type: str,
    session: Session = Depends(get_session),
):
    """
    Check availability for breakdown & repair services (no time slots).
    Functionality: Get availability status for breakdown services at a center
    """
    service = session.exec(
        select(CenterService).where(
            CenterService.service_center_id == center_id,
            CenterService.service_type == service_type,
        )
    ).first()

    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    if service.service_type != "breakdown_repair":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is for breakdown & repair services only",
        )

    return {
        "service_type": service.service_type,
        "is_available": service.is_available,
        "message": "Available" if service.is_available else "Currently unavailable",
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
    Functionality: User creates service booking (slot-based or breakdown)
    """
    if current_user.role != "user":
        raise HTTPException(status_code=403, detail="Only users can book services")

    # Verify service center exists and is available
    center = session.get(ServiceCenter, booking_data.service_center_id)
    if not center or center.status != "available":
        raise HTTPException(status_code=404, detail="Service center not available")

    # Verify service exists at center
    service = session.get(CenterService, booking_data.center_service_id)
    if not service or service.service_center_id != booking_data.service_center_id:
        raise HTTPException(status_code=404, detail="Service not found at this center")

    # Service type validation
    if service.service_type != booking_data.service_type:
        raise HTTPException(status_code=400, detail="Service type mismatch")

    # For slot-based services: verify slot and update capacity
    if service.service_type != "breakdown_repair":
        if not booking_data.slot_id:
            raise HTTPException(
                status_code=400, detail="Slot ID required for slot-based services"
            )

        # MODIFIED: Lock the row with_for_update() to prevent race conditions
        try:
            statement = (
                select(ServiceSlot)
                .where(ServiceSlot.id == booking_data.slot_id)
                .with_for_update()
            )
            slot = session.exec(statement).one()
        except NoResultFound:
            raise HTTPException(status_code=404, detail="Slot not found")

        if slot.center_service_id != booking_data.center_service_id:
            raise HTTPException(status_code=404, detail="Slot does not match service")

        if slot.booked_count >= slot.capacity:
            session.rollback()  # Release lock
            raise HTTPException(
                status_code=400, detail="No capacity remaining in this slot"
            )

        # Update slot booking count
        slot.booked_count += 1
        session.add(slot)

        # Calculate expected return date based on service duration
        expected_return_datetime = slot.end_time + timedelta(
            hours=service.expected_duration_hours
        )
        expected_return_date = expected_return_datetime.date()
        expected_return_time = expected_return_datetime.strftime("%H:%M")
    else:
        # For breakdown: no slot, no auto-calculated return
        expected_return_date = None
        expected_return_time = None

    # Create booking
    new_booking = ServiceRequest(
        user_id=current_user.id,
        **booking_data.model_dump(),
        status="booked",
        booking_time=datetime.utcnow(),
        expected_return_date=expected_return_date,
        expected_return_time=expected_return_time,
        price_at_booking=service.price,
    )

    session.add(new_booking)
    session.commit()
    session.refresh(new_booking)

    # Notify service center about new booking
    background_tasks.add_task(
        send_push_notification,
        session=session,
        user_ids=[center.user_id],
        title="New Booking 📅",
        body=f"New {booking_data.service_type} booking from customer",
        data={"booking_id": new_booking.id, "type": "new_booking"},
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
    Functionality: User views their own service bookings with optional status filter
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


@router.patch("/my-bookings/{booking_id}/cancel")
def cancel_service_booking(
    booking_id: int,
    background_tasks: BackgroundTasks,
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Cancel a service booking.
    Functionality: User cancels their booking and frees up slot capacity
    """
    booking = session.get(ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status in ["completed", "cancelled"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel a completed or already cancelled booking",
        )

    # Free up slot capacity if slot-based
    if booking.slot_id:
        try:
            statement = (
                select(ServiceSlot)
                .where(ServiceSlot.id == booking.slot_id)
                .with_for_update()
            )
            slot = session.exec(statement).one()

            if slot.booked_count > 0:
                slot.booked_count -= 1
                session.add(slot)
        except NoResultFound:
            pass

    booking.status = "cancelled"
    session.add(booking)
    session.commit()

    # Notify service center
    center = session.get(ServiceCenter, booking.service_center_id)
    if center:
        background_tasks.add_task(
            send_push_notification,
            session=session,
            user_ids=[center.user_id],
            title="Booking Cancelled ❌",
            body="A customer cancelled their service booking",
            data={"booking_id": booking.id, "type": "cancellation"},
        )

    return {"message": "Booking cancelled successfully"}


# --- REVIEWS ---


@router.post("/my-bookings/{booking_id}/review")
def submit_service_review(
    booking_id: int,
    rating: int = Query(..., ge=1, le=5),
    comment: str = Query(None),
    *,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Submit a review for a completed service.
    Functionality: User rates and reviews service center after service completion
    """
    booking = session.get(ServiceRequest, booking_id)
    if not booking or booking.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status != "completed":
        raise HTTPException(
            status_code=400, detail="Can only review completed bookings"
        )

    # Check if review already exists
    existing_review = session.exec(
        select(ServiceCenterReview).where(
            ServiceCenterReview.user_id == current_user.id,
            ServiceCenterReview.service_request_id == booking_id,
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
        service_request_id=booking_id,
        rating=rating,
        comment=comment,
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
        "rating": rating,
        "center_new_rating": center.rating,
    }


@router.get("/centers/{center_id}/reviews")
def get_service_center_reviews(
    center_id: int,
    session: Session = Depends(get_session),
    page: int = Query(1, gt=0),
    limit: int = Query(10, gt=0, le=50),
):
    """
    Get all reviews for a service center.
    Functionality: View user reviews and ratings for a service center
    """
    center = session.get(ServiceCenter, center_id)
    if not center:
        raise HTTPException(status_code=404, detail="Service center not found")

    offset = (page - 1) * limit
    reviews = session.exec(
        select(ServiceCenterReview)
        .where(ServiceCenterReview.service_center_id == center_id)
        .order_by(desc(ServiceCenterReview.created_at))
        .offset(offset)
        .limit(limit)
    ).all()

    return [
        {
            "id": r.id,
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at,
        }
        for r in reviews
    ]
