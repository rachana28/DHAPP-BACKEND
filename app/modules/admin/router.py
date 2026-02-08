import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, func, desc, delete
from typing import List, Optional
import redis
from datetime import datetime

from app.core.database import get_session, get_redis
from app.core.models import (
    User,
    UserPublic,
    Driver,
    DriverPrivate,
    TowTruckDriver,
    TowTruckDriverPrivate,
    Trip,
    TripOffer,
    TowTripOffer,
    UserDevice,
    TripSafe,
    SystemConfig,
    SupportTicket,
    SupportTicketResponse,
)
from app.core.security import get_current_admin

# Protect ENTIRE router with Admin check
router = APIRouter(
    prefix="/admin", tags=["Admin Dashboard"], dependencies=[Depends(get_current_admin)]
)


# --- 1. DASHBOARD OVERVIEW ---
@router.get("/dashboard-stats")
def get_dashboard_stats(session: Session = Depends(get_session)):
    """
    Aggregated stats including BOTH Cab Drivers and Tow Drivers.
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

    # 3. Count Tow Drivers (NEW)
    tow_drivers = session.exec(select(func.count(TowTruckDriver.id))).one()
    pending_tow = session.exec(
        select(func.count(TowTruckDriver.id)).where(
            TowTruckDriver.status == "pending_approval"
        )
    ).one()

    # 4. Combined Stats
    total_drivers = cab_drivers + tow_drivers
    total_pending = pending_cab + pending_tow

    # 5. Trips
    completed_trips = session.exec(
        select(func.count(Trip.id)).where(Trip.status == "completed")
    ).one()

    return {
        "total_users": total_users,
        "total_drivers": total_drivers,  # Sum of both
        "pending_reviews": total_pending,  # Sum of both
        "total_trips": completed_trips,
        # Optional: specific breakdown if needed by frontend later
        "breakdown": {"cab_drivers": cab_drivers, "tow_drivers": tow_drivers},
    }


# --- 2. DRIVER MANAGEMENT (Review Flow) ---
@router.get("/drivers", response_model=List[DriverPrivate])
def get_drivers_admin(
    status: Optional[str] = None,  # e.g., 'pending_approval'
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
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
    session.add(driver)
    session.commit()

    # CRITICAL: Invalidate public cache so they appear/disappear immediately
    if redis_client:
        redis_client.delete("drivers")
        redis_client.delete(f"driver_{driver.id}")

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
    session: Session = Depends(get_session),
):
    driver = session.get(TowTruckDriver, driver_id)
    if not driver:
        raise HTTPException(404, "Tow Driver not found")

    driver.status = status
    session.add(driver)
    session.commit()
    return {"message": f"Tow Driver status updated to {status}"}


# --- 4. USER MANAGEMENT ---
@router.get("/users", response_model=List[UserPublic])
def get_users_admin(
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
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
    user_id: int,
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

        # 2. Delete Support Tickets
        session.exec(delete(SupportTicket).where(SupportTicket.user_id == user_id))

        # 3. Handle Trips (As a Rider)
        # Option A: Delete all their trips (Cleaner for dev)
        # Option B: Set user_id to NULL (Requires nullable FK in DB)
        # We will go with Option A to ensure clean deletion.
        session.exec(delete(Trip).where(Trip.user_id == user_id))

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

        # 6. Finally, Delete the User
        session.delete(user)
        session.commit()

        return {"message": "User and all associated data deleted successfully"}

    except Exception as e:
        session.rollback()
        # Log the specific DB error for debugging
        print(f"Error deleting user: {e}")
        raise HTTPException(500, f"Database Constraint Error: {str(e)}")


# --- 5. TRIP OVERSIGHT ---
@router.get("/trips", response_model=List[TripSafe])
def get_all_trips_admin(
    skip: int = 0, limit: int = 50, session: Session = Depends(get_session)
):
    return session.exec(
        select(Trip).order_by(desc(Trip.booking_time)).offset(skip).limit(limit)
    ).all()


@router.get("/users/{user_id}/trips")
def get_user_trip_history(
    user_id: int,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
):
    """
    Fetches ALL trips for a specific user from the single 'Trip' table.
    Differentiates between 'Ride' and 'Tow' using 'hiring_type'.
    """
    # 1. Fetch ALL trips for the user in one query, sorted by time
    trips = session.exec(
        select(Trip).where(Trip.user_id == user_id).order_by(desc(Trip.booking_time))
    ).all()

    # 2. Normalize Data for Frontend Table
    history = []

    for t in trips:
        # Determine Service Type
        # If hiring_type is "Tow Service", categorize as Tow
        service_type = "Tow" if t.hiring_type == "Tow Service" else "Ride"

        # Determine specific Driver ID (Tow Driver or Regular Driver)
        assigned_driver_id = (
            t.tow_truck_driver_id if service_type == "Tow" else t.driver_id
        )

        # Generate a unique display ID (e.g., TOW-101 or RIDE-101)
        display_id = f"{service_type.upper()}-{t.id}"

        history.append(
            {
                "id": display_id,  # Composite ID for Frontend keys
                "original_id": t.id,  # Real DB ID
                "service_type": service_type,
                "booking_time": t.booking_time,
                "status": t.status,
                "price": t.fare if t.fare else 0.0,
                "source": t.start_location or "N/A",
                "destination": t.end_location or "N/A",
                "driver_id": assigned_driver_id,
                "vehicle_type": t.vehicle_type,  # Useful extra info
            }
        )

    return history


# --- SUPPORT TICKET MANAGEMENT ---
@router.get("/tickets", response_model=List[SupportTicketResponse])
def get_all_tickets(
    status: Optional[str] = None,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    query = select(SupportTicket).order_by(desc(SupportTicket.created_at))
    if status:
        query = query.where(SupportTicket.status == status)
    if category:
        query = query.where(SupportTicket.category == category)

    return session.exec(query.offset(skip).limit(limit)).all()


@router.patch("/tickets/{ticket_db_id}/resolve")
def resolve_ticket(
    ticket_db_id: int,
    status: str = Query(..., regex="^(open|in_progress|resolved|closed)$"),
    admin_response: str = Query(...),
    session: Session = Depends(get_session),
):
    ticket = session.get(SupportTicket, ticket_db_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    ticket.status = status
    ticket.admin_response = admin_response
    ticket.updated_at = datetime.utcnow()

    session.add(ticket)
    session.commit()

    # Optional: Send Push Notification to User about update

    return {"message": "Ticket updated successfully", "ticket": ticket}


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
    pricing_keys = ["base_fare", "rate_per_km", "min_charge"]
    if any(pk in key for pk in pricing_keys):
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
