import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, func, desc, delete
from typing import List, Optional
import redis
from datetime import datetime

from app.core.database import get_session, get_redis
from app.core.models import (
    CenterService,
    User,
    UserPublic,
    Driver,
    DriverPrivate,
    TowTruckDriver,
    TowTruckDriverPrivate,
    ServiceCenter,
    ServiceCenterPrivate,
    Trip,
    TripOffer,
    TowTrip,
    TowTripOffer,
    MechanicTrip,
    UserDevice,
    TripSafe,
    SystemConfig,
    SupportTicket,
    SupportAttachment,
    UITheme,
    UIBanner,
    Mechanic,
    MechanicOffer,
    MechanicReview,
    ServiceRequest,
    ServiceSlot,
    ServiceCenterReview,
)
from app.utils.storage import _delete_r2_keys_sync
from app.core.security import get_current_admin
from app.utils.notifications import send_push_notification

# Protect ENTIRE router with Admin check
router = APIRouter(
    prefix="/admin", tags=["Admin Dashboard"], dependencies=[Depends(get_current_admin)]
)


# --- 1. DASHBOARD OVERVIEW ---
@router.get("/dashboard-stats")
def get_dashboard_stats(session: Session = Depends(get_session)):
    """
    Aggregated stats including BOTH Cab Drivers, Tow Drivers, and Service Centers.
    Functionality: Dashboard overview with user, driver, and service center statistics
    """
    # 1. Count Users
    total_users = session.exec(
        select(func.count(User.id)).where(User.role == "user")
    ).one()

    # 2. Count Cab Drivers
    cab_drivers = session.exec(select(func.count(Driver.id))).one()
    pending_cab = session.exec(
        select(func.count(Driver.id)).where(Driver.status == "pending_approval")
    ).one()

    # 3. Count Tow Drivers
    tow_drivers = session.exec(select(func.count(TowTruckDriver.id))).one()
    pending_tow = session.exec(
        select(func.count(TowTruckDriver.id)).where(
            TowTruckDriver.status == "pending_approval"
        )
    ).one()

    mechanics = session.exec(select(func.count(Mechanic.id))).one()
    pending_mechanics = session.exec(
        select(func.count(Mechanic.id)).where(Mechanic.status == "pending_approval")
    ).one()

    # 4. Count Service Centers
    service_centers = session.exec(select(func.count(ServiceCenter.id))).one()
    pending_service_centers = session.exec(
        select(func.count(ServiceCenter.id)).where(
            ServiceCenter.status == "pending_approval"
        )
    ).one()

    # 5. Combined Stats
    total_drivers = cab_drivers + tow_drivers + mechanics
    total_pending = (
        pending_cab + pending_tow + pending_service_centers + pending_mechanics
    )

    # 6. Trips — sum across all three trip tables (rides, tow, mechanic)
    completed_rides = session.exec(
        select(func.count(Trip.id)).where(Trip.status == "completed")
    ).one()
    completed_tow = session.exec(
        select(func.count(TowTrip.id)).where(TowTrip.status == "completed")
    ).one()
    completed_mech = session.exec(
        select(func.count(MechanicTrip.id)).where(MechanicTrip.status == "completed")
    ).one()
    completed_trips = completed_rides + completed_tow + completed_mech

    return {
        "total_users": total_users,
        "total_drivers": total_drivers,  # Sum of cab and tow drivers
        "total_service_centers": service_centers,
        "pending_reviews": total_pending,  # Sum of all pending approvals
        "total_trips": completed_trips,
        # Optional: specific breakdown if needed by frontend later
        "breakdown": {
            "cab_drivers": cab_drivers,
            "tow_drivers": tow_drivers,
            "service_centers": service_centers,
            "pending_cab_drivers": pending_cab,
            "pending_tow_drivers": pending_tow,
            "pending_service_centers": pending_service_centers,
            "mechanics": mechanics,
            "pending_mechanics": pending_mechanics,
        },
    }


# --- 2. DRIVER MANAGEMENT (Review Flow) ---
@router.get("/drivers", response_model=List[DriverPrivate])
def get_drivers_admin(
    status: Optional[str] = None,  # e.g., 'pending_approval'
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
):
    query = select(Driver)
    if status:
        query = query.where(Driver.status == status)
    if search:
        query = query.where(Driver.name.contains(search))

    return session.exec(query.offset(skip).limit(limit)).all()


@router.patch("/drivers/{driver_id}/status")
def update_driver_status(
    driver_id: int,
    status: str = Query(..., regex="^(available|banned|pending_approval|rejected)$"),
    admin_notes: Optional[str] = Query(None),
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Approve or Reject a driver.
    """
    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(404, "Driver not found")

    driver.status = status

    if admin_notes:
        driver.admin_notes = admin_notes
    elif status == "available":
        driver.admin_notes = None

    session.add(driver)
    session.commit()

    # CRITICAL: Invalidate public cache so they appear/disappear immediately
    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{driver.id}")

    if status in ["rejected", "banned"]:
        send_push_notification(
            session=session,
            user_ids=[driver.user_id],
            title="Account Status Update",
            body=f"Your profile has been {status} by the administrator.",
            data={"type": "account_restricted"},
        )

    return {"message": f"Driver status updated to {status}"}


# --- 3. TOW DRIVER MANAGEMENT ---
@router.get("/tow-drivers", response_model=List[TowTruckDriverPrivate])
def get_tow_drivers_admin(
    status: Optional[str] = None, session: Session = Depends(get_session)
):
    query = select(TowTruckDriver)
    if status:
        query = query.where(TowTruckDriver.status == status)
    return session.exec(query).all()


@router.patch("/tow-drivers/{driver_id}/status")
def update_tow_driver_status(
    driver_id: int,
    status: str = Query(..., regex="^(available|banned|pending_approval|rejected)$"),
    admin_notes: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    driver = session.get(TowTruckDriver, driver_id)
    if not driver:
        raise HTTPException(404, "Tow Driver not found")

    driver.status = status

    if admin_notes:
        driver.admin_notes = admin_notes
    elif status == "available":
        driver.admin_notes = None

    session.add(driver)
    session.commit()

    if status in ["rejected", "banned"]:
        send_push_notification(
            session=session,
            user_ids=[driver.user_id],
            title="Account Status Update",
            body=f"Your profile has been {status} by the administrator.",
            data={"type": "account_restricted"},
        )

    return {"message": f"Tow Driver status updated to {status}"}


# --- 3.5 MECHANIC MANAGEMENT ---
@router.get("/mechanics")
def get_mechanics_admin(
    status: Optional[str] = None, session: Session = Depends(get_session)
):
    query = select(Mechanic)
    if status:
        query = query.where(Mechanic.status == status)
    return session.exec(query).all()


@router.patch("/mechanics/{mechanic_id}/status")
def update_mechanic_status(
    mechanic_id: int,
    status: str = Query(..., regex="^(available|banned|pending_approval|rejected)$"),
    admin_notes: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    mechanic = session.get(Mechanic, mechanic_id)
    if not mechanic:
        raise HTTPException(404, "Mechanic not found")

    mechanic.status = status

    if admin_notes:
        mechanic.admin_notes = admin_notes
    elif status == "available":
        mechanic.admin_notes = None

    session.add(mechanic)
    session.commit()

    if status in ["rejected", "banned"]:
        send_push_notification(
            session=session,
            user_ids=[mechanic.user_id],
            title="Account Status Update",
            body=f"Your profile has been {status} by the administrator.",
            data={"type": "account_restricted"},
        )

    return {"message": f"Mechanic status updated to {status}"}


# --- 4. SERVICE CENTER MANAGEMENT ---
@router.get("/service-centers", response_model=List[ServiceCenterPrivate])
def get_service_centers_admin(
    status: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
):
    """
    Get all service centers with optional filters.
    Functionality: Admin views all service centers, filtered by status or search
    """
    query = select(ServiceCenter)
    if status:
        query = query.where(ServiceCenter.status == status)
    if search:
        query = query.where(ServiceCenter.name.contains(search))

    centers = session.exec(query.offset(skip).limit(limit)).all()
    return [ServiceCenterPrivate(**c.model_dump()) for c in centers]


@router.patch("/service-centers/{center_id}/status")
def update_service_center_status(
    center_id: int,
    status: str = Query(..., regex="^(available|banned|pending_approval|rejected)$"),
    admin_notes: Optional[str] = Query(None),
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Approve or Reject a service center.
    Functionality: Admin approves/rejects service center signup applications
    """
    center = session.get(ServiceCenter, center_id)
    if not center:
        raise HTTPException(404, "Service center not found")

    center.status = status

    if admin_notes:
        center.admin_notes = admin_notes
    elif status == "available":
        center.admin_notes = None

    session.add(center)

    if status in ["banned", "rejected"]:
        active_bookings = session.exec(
            select(ServiceRequest).where(
                ServiceRequest.service_center_id == center.id,
                ServiceRequest.status.in_(["booked", "checked_in", "in_service"]),
            )
        ).all()

        for booking in active_bookings:
            booking.status = "cancelled"
            session.add(booking)

            # Revert slot capacity if applicable
            if booking.slot_id:
                slot = session.get(ServiceSlot, booking.slot_id)
                if slot and slot.booked_count > 0:
                    slot.booked_count -= 1
                    session.add(slot)

            send_push_notification(
                session,
                [booking.user_id],
                "Booking Cancelled",
                "The service center is no longer available.",
            )

    session.commit()

    # Invalidate cache so changes reflect immediately
    if redis_client:
        redis_client.delete("service_centers")
        redis_client.delete(f"service_center_{center.id}")

    return {"message": f"Service center status updated to {status}"}


# --- VERIFICATION DETAILS ---
@router.get("/verification-details/{role}/{profile_id}")
def get_verification_details(
    role: str, profile_id: int, session: Session = Depends(get_session)
):
    """
    Fetch documents and missing fields for profile validation.
    Used by Admin to approve/reject pending registrations.
    """
    if role == "driver":
        profile = session.get(Driver, profile_id)
    elif role == "tow_truck_driver":
        profile = session.get(TowTruckDriver, profile_id)
    elif role == "mechanic":
        profile = session.get(Mechanic, profile_id)
    elif role == "service_center":
        profile = session.get(ServiceCenter, profile_id)
    else:
        raise HTTPException(
            400,
            "Invalid role parameter. Use driver, tow_truck_driver, mechanic, or service_center.",
        )

    if not profile:
        raise HTTPException(404, f"{role} profile not found")

    return {
        "profile_id": profile.id,
        "role": role,
        "name": profile.name,
        "status": profile.status,
        "profile_picture": profile.profile_picture_url,
        "phone_number": profile.phone_number,
        "verification_documents": getattr(profile, "verification_documents", []),
        "details": profile.model_dump(),
    }


# --- 4. USER MANAGEMENT ---
@router.get("/users", response_model=List[UserPublic])
def get_users_admin(
    search: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
):
    query = select(User).where(User.role == "user")
    if search:
        query = query.where(
            User.email.contains(search) | User.full_name.contains(search)
        )
    return session.exec(query.offset(skip).limit(limit)).all()


@router.delete("/users/{user_id}")
def delete_user(
    user_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    # Safety Check: Prevent deleting Super Admin
    super_admin_email = os.getenv("SUPER_ADMIN_EMAIL")

    if super_admin_email and user.email == super_admin_email:
        raise HTTPException(400, "Cannot delete Super Admin.")

    if user.id == current_admin.id:
        raise HTTPException(400, "You cannot delete your own account.")

    try:
        # 1. Delete User Devices (Push Tokens)
        session.exec(delete(UserDevice).where(UserDevice.user_id == user_id))

        # 2. Delete Support Tickets — first collect R2 keys for their
        # attachments so we can clean up storage AFTER the DB transaction
        # succeeds. The cascade on the SupportTicket relationships removes
        # SupportMessage + SupportAttachment rows automatically.
        ticket_attachment_keys = session.exec(
            select(SupportAttachment.r2_key)
            .join(SupportTicket, SupportAttachment.ticket_id == SupportTicket.id)
            .where(SupportTicket.user_id == user_id)
        ).all()
        tickets_to_delete = session.exec(
            select(SupportTicket).where(SupportTicket.user_id == user_id)
        ).all()
        for t in tickets_to_delete:
            session.delete(t)

        # 3. Handle Trips (As a Rider) — cascade across all three trip tables.
        # Each trip's child offers must be deleted first to satisfy FK constraints.
        # Subquery is evaluated at delete time so a trip inserted between SELECT
        # and DELETE can't slip through and leave an orphan offer.
        session.exec(
            delete(TripOffer).where(
                TripOffer.trip_id.in_(select(Trip.id).where(Trip.user_id == user_id))
            )
        )
        session.exec(delete(Trip).where(Trip.user_id == user_id))

        session.exec(
            delete(TowTripOffer).where(
                TowTripOffer.trip_id.in_(
                    select(TowTrip.id).where(TowTrip.user_id == user_id)
                )
            )
        )
        session.exec(delete(TowTrip).where(TowTrip.user_id == user_id))

        session.exec(
            delete(MechanicOffer).where(
                MechanicOffer.trip_id.in_(
                    select(MechanicTrip.id).where(MechanicTrip.user_id == user_id)
                )
            )
        )
        session.exec(delete(MechanicTrip).where(MechanicTrip.user_id == user_id))

        # 4. Handle Driver Profile (If they are a Cab Driver)
        driver = session.exec(select(Driver).where(Driver.user_id == user_id)).first()
        if driver:
            # Delete Driver's Offers
            session.exec(delete(TripOffer).where(TripOffer.driver_id == driver.id))
            # Unlink trips where they were the driver (Set driver_id to None or delete)
            # For simplicity in this fix, we delete the profile.
            # Note: If they have active trips as a driver, this might fail unless we clear those too.
            session.delete(driver)

        # 5. Handle Tow Driver Profile (If they are a Tow Driver)
        tow_driver = session.exec(
            select(TowTruckDriver).where(TowTruckDriver.user_id == user_id)
        ).first()
        if tow_driver:
            session.exec(
                delete(TowTripOffer).where(
                    TowTripOffer.tow_truck_driver_id == tow_driver.id
                )
            )
            session.delete(tow_driver)

        mechanic = session.exec(
            select(Mechanic).where(Mechanic.user_id == user_id)
        ).first()
        if mechanic:
            session.exec(
                delete(MechanicOffer).where(MechanicOffer.mechanic_id == mechanic.id)
            )
            session.exec(
                delete(MechanicReview).where(MechanicReview.mechanic_id == mechanic.id)
            )
            session.delete(mechanic)

        # 6. Handle Service Center Profile (If they are a Service Center)
        service_center = session.exec(
            select(ServiceCenter).where(ServiceCenter.user_id == user_id)
        ).first()
        if service_center:
            # Delete Center's Reviews, Bookings, Slots, and Services
            session.exec(
                delete(ServiceCenterReview).where(
                    ServiceCenterReview.service_center_id == service_center.id
                )
            )
            session.exec(
                delete(ServiceRequest).where(
                    ServiceRequest.service_center_id == service_center.id
                )
            )
            session.exec(
                delete(ServiceSlot).where(
                    ServiceSlot.service_center_id == service_center.id
                )
            )
            session.exec(
                delete(CenterService).where(
                    CenterService.service_center_id == service_center.id
                )
            )
            # Delete the Center itself
            session.delete(service_center)

        # 7. Finally, Delete the User
        session.delete(user)
        session.commit()

        # Best-effort R2 cleanup for any support attachments that belonged
        # to the deleted user. Runs after DB commit so a storage outage
        # doesn't roll back the deletion.
        keys = [k for k in (ticket_attachment_keys or []) if k]
        if keys:
            try:
                _delete_r2_keys_sync(keys)
            except Exception as e:
                print(f"R2 cleanup on user delete failed: {e}")

        return {"message": "User and all associated data deleted successfully"}

    except Exception as e:
        session.rollback()
        # Log the specific DB error for debugging
        print(f"Error deleting user: {e}")
        raise HTTPException(500, f"Database Constraint Error: {str(e)}")


# --- 5. TRIP OVERSIGHT ---
def _trip_to_safe_dict(t: Trip) -> dict:
    return {
        "id": t.id,
        "hiring_type": t.hiring_type,
        "vehicle_type": t.vehicle_type,
        "shift_details": t.shift_details,
        "start_date": t.start_date,
        "end_date": t.end_date,
        "months": t.months,
        "selected_days": t.selected_days,
        "start_location": t.start_location,
        "end_location": t.end_location,
        "reason": t.reason,
        "status": t.status,
        "fare": t.fare,
        "fare_breakdown": t.fare_breakdown,
        "start_lat": t.start_lat,
        "start_lng": t.start_lng,
        "end_lat": t.end_lat,
        "end_lng": t.end_lng,
        "distance_km": t.distance_km,
        "booking_time": t.booking_time,
    }


def _mech_trip_to_safe_dict(t: MechanicTrip) -> dict:
    return {
        "id": t.id,
        "hiring_type": "Mechanic Service",
        "vehicle_type": t.vehicle_type,
        "shift_details": None,
        "start_date": None,
        "end_date": None,
        "months": None,
        "selected_days": None,
        "start_location": t.start_location,
        "end_location": None,
        "reason": t.reason,
        "status": t.status,
        "fare": t.fare,
        "fare_breakdown": t.fare_breakdown,
        "start_lat": t.start_lat,
        "start_lng": t.start_lng,
        "end_lat": None,
        "end_lng": None,
        "distance_km": None,
        "booking_time": t.booking_time,
    }


def _tow_trip_to_safe_dict(t: TowTrip) -> dict:
    return {
        "id": t.id,
        "hiring_type": "Tow Service",
        "vehicle_type": t.vehicle_type,
        "shift_details": None,
        "start_date": None,
        "end_date": None,
        "months": None,
        "selected_days": None,
        "start_location": t.start_location,
        "end_location": t.end_location,
        "reason": t.reason,
        "status": t.status,
        "fare": t.fare,
        "fare_breakdown": t.fare_breakdown,
        "start_lat": t.start_lat,
        "start_lng": t.start_lng,
        "end_lat": t.end_lat,
        "end_lng": t.end_lng,
        "distance_km": t.distance_km,
        "booking_time": t.booking_time,
    }


@router.get("/trips", response_model=List[TripSafe])
def get_all_trips_admin(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
):
    rides = session.exec(select(Trip).order_by(desc(Trip.booking_time))).all()
    tows = session.exec(select(TowTrip).order_by(desc(TowTrip.booking_time))).all()
    mechs = session.exec(
        select(MechanicTrip).order_by(desc(MechanicTrip.booking_time))
    ).all()

    combined = (
        [_trip_to_safe_dict(t) for t in rides]
        + [_tow_trip_to_safe_dict(t) for t in tows]
        + [_mech_trip_to_safe_dict(t) for t in mechs]
    )
    combined.sort(key=lambda r: r["booking_time"], reverse=True)
    return combined[skip : skip + limit]


@router.get("/users/{user_id}/trips")
def get_user_trip_history(
    user_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
):
    """
    Fetches ALL trips for a specific user across rides, tow, mechanic, and
    vehicle service bookings. Each kind lives in its own table now; the
    `service_type` discriminator is set per source table.
    """
    # 1. Ride trips (Daily / Monthly / Outstation)
    rides = session.exec(
        select(Trip).where(Trip.user_id == user_id).order_by(desc(Trip.booking_time))
    ).all()

    # 2. Tow trips
    tow_trips = session.exec(
        select(TowTrip)
        .where(TowTrip.user_id == user_id)
        .order_by(desc(TowTrip.booking_time))
    ).all()

    # 3. Mechanic trips
    mech_trips = session.exec(
        select(MechanicTrip)
        .where(MechanicTrip.user_id == user_id)
        .order_by(desc(MechanicTrip.booking_time))
    ).all()

    # 4. Service center bookings
    service_requests = session.exec(
        select(ServiceRequest)
        .where(ServiceRequest.user_id == user_id)
        .order_by(desc(ServiceRequest.booking_time))
    ).all()

    history = []

    # Process Ride Trips
    for t in rides:
        history.append(
            {
                "id": f"RIDE-{t.id}",
                "original_id": t.id,
                "service_type": "Ride",
                "booking_time": t.booking_time,
                "status": t.status,
                "price": t.fare if t.fare else 0.0,
                "source": t.start_location or "N/A",
                "destination": t.end_location or "N/A",
                "driver_or_center_id": t.driver_id,
                "vehicle_type": t.vehicle_type,
            }
        )

    # Process Tow Trips
    for t in tow_trips:
        history.append(
            {
                "id": f"TOW-{t.id}",
                "original_id": t.id,
                "service_type": "Tow",
                "booking_time": t.booking_time,
                "status": t.status,
                "price": t.fare if t.fare else 0.0,
                "source": t.start_location or "N/A",
                "destination": t.end_location or "N/A",
                "driver_or_center_id": t.tow_truck_driver_id,
                "vehicle_type": t.vehicle_type,
            }
        )

    # Process Mechanic Trips
    for t in mech_trips:
        history.append(
            {
                "id": f"MECHANIC-{t.id}",
                "original_id": t.id,
                "service_type": "Mechanic Service",
                "booking_time": t.booking_time,
                "status": t.status,
                "price": t.fare if t.fare else 0.0,
                "source": t.start_location or "N/A",
                "destination": "N/A",
                "driver_or_center_id": t.mechanic_id,
                "vehicle_type": t.vehicle_type,
            }
        )

    # Process Service Requests
    for sr in service_requests:
        history.append(
            {
                "id": f"SERVICE-{sr.id}",
                "original_id": sr.id,
                "service_type": "Vehicle Service",
                "booking_time": sr.booking_time,
                "status": sr.status,
                "price": sr.final_price if sr.final_price else 0.0,
                "source": sr.service_name,
                "destination": "Garage Drop-off",
                "driver_or_center_id": sr.service_center_id,
                "vehicle_type": sr.vehicle_type,
            }
        )

    # Sort combined history by booking_time descending
    history.sort(key=lambda x: x["booking_time"], reverse=True)
    return history


# --- SUPPORT TICKET MANAGEMENT moved to app/modules/support/admin_router.py ---


# --- SYSTEM CONFIGURATION (PRICING & SETTINGS) ---
@router.get("/system-config")
def get_system_config(session: Session = Depends(get_session)):
    """
    Get all dynamic system settings (e.g. Base Fare).
    """
    configs = session.exec(select(SystemConfig)).all()
    # Convert list to simple dict for frontend
    return {c.key: c.value for c in configs}


@router.post("/system-config")
def update_system_config(
    key: str,
    value: str,
    description: Optional[str] = None,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Update or Create a system setting.
    """
    # Any key whose name starts with one of these prefixes is treated as numeric.
    numeric_prefixes = ("pricing_", "state_permit_")
    pricing_keys = ["base_fare", "rate_per_km", "min_charge", "driver_acceptance_fee"]
    if key.startswith(numeric_prefixes) or any(pk in key for pk in pricing_keys):
        try:
            float(value)  # Try casting to ensure it's a number
        except ValueError:
            raise HTTPException(
                400, "Value for pricing must be a valid number (e.g. '12.5')"
            )

    config = session.get(SystemConfig, key)
    if not config:
        config = SystemConfig(key=key, value=value, description=description)
    else:
        config.value = value
        if description:
            config.description = description

    session.add(config)
    session.commit()

    # Update Redis Cache (So pricing algo is fast)
    if redis_client:
        redis_client.set(f"config:{key}", value)

    return {"message": f"Config '{key}' updated to '{value}'"}


# --- APP CONFIGURATION (THEMES) ---


@router.get("/themes", response_model=List[UITheme])
def get_all_themes(session: Session = Depends(get_session)):
    """Get all UI themes (Seasons and Festivals)"""
    return session.exec(select(UITheme)).all()


@router.post("/themes", response_model=UITheme)
def create_theme(theme: UITheme, session: Session = Depends(get_session)):
    """Create a new UI theme"""
    session.add(theme)
    session.commit()
    session.refresh(theme)
    return theme


@router.put("/themes/{theme_id}", response_model=UITheme)
def update_theme(
    theme_id: int, theme_data: dict, session: Session = Depends(get_session)
):
    """Update an existing UI theme"""
    theme = session.get(UITheme, theme_id)
    if not theme:
        raise HTTPException(404, "Theme not found")

    for key, value in theme_data.items():
        if hasattr(theme, key):
            setattr(theme, key, value)

    session.add(theme)
    session.commit()
    session.refresh(theme)
    return theme


@router.delete("/themes/{theme_id}")
def delete_theme(theme_id: int, session: Session = Depends(get_session)):
    """Delete a UI theme"""
    theme = session.get(UITheme, theme_id)
    if not theme:
        raise HTTPException(404, "Theme not found")
    session.delete(theme)
    session.commit()
    return {"message": "Theme deleted successfully"}


# --- APP CONFIGURATION (BANNERS) ---


@router.get("/banners", response_model=List[UIBanner])
def get_all_banners(session: Session = Depends(get_session)):
    """Get all promotional auto-scroll banners"""
    return session.exec(select(UIBanner)).all()


@router.post("/banners", response_model=UIBanner)
def create_banner(banner: UIBanner, session: Session = Depends(get_session)):
    """Create a new banner"""
    session.add(banner)
    session.commit()
    session.refresh(banner)
    return banner


@router.put("/banners/{banner_id}", response_model=UIBanner)
def update_banner(
    banner_id: int, banner_data: dict, session: Session = Depends(get_session)
):
    """Update an existing banner"""
    banner = session.get(UIBanner, banner_id)
    if not banner:
        raise HTTPException(404, "Banner not found")

    for key, value in banner_data.items():
        if hasattr(banner, key):
            setattr(banner, key, value)

    session.add(banner)
    session.commit()
    session.refresh(banner)
    return banner


@router.delete("/banners/{banner_id}")
def delete_banner(banner_id: int, session: Session = Depends(get_session)):
    """Delete a banner"""
    banner = session.get(UIBanner, banner_id)
    if not banner:
        raise HTTPException(404, "Banner not found")
    session.delete(banner)
    session.commit()
    return {"message": "Banner deleted successfully"}
