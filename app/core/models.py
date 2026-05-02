import uuid
import html
from enum import Enum
from pydantic import EmailStr, field_validator
from sqlmodel import Field, SQLModel, Relationship
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from sqlalchemy import UniqueConstraint, JSON, Column


# --- Base Models (Shared fields) ---
class DriverBase(SQLModel):
    name: str
    phone_number: str
    license_number: Optional[str] = None
    address: Optional[str] = None
    emergency_phone: Optional[str] = None
    profile_picture_url: Optional[str] = None
    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None
    fare_per_km: Optional[float] = None
    driver_allowance: Optional[float] = None
    spoken_languages: Optional[str] = None
    status: str = "pending_approval"
    verification_documents: List[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    admin_notes: Optional[str] = None


class TowTruckDriverBase(SQLModel):
    name: str
    phone_number: str
    vehicle_number: Optional[str] = None
    address: Optional[str] = None
    profile_picture_url: Optional[str] = None
    status: str = "pending_approval"
    rating: float = Field(default=0.0)
    verification_documents: List[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    admin_notes: Optional[str] = None


# --- MECHANIC MODELS ADDITIONS ---


class MechanicBase(SQLModel):
    name: str
    phone_number: str
    specialization: Optional[str] = None
    address: Optional[str] = None
    profile_picture_url: Optional[str] = None
    status: str = "pending_approval"
    rating: float = Field(default=0.0)
    verification_documents: List[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    admin_notes: Optional[str] = None


class Mechanic(MechanicBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")

    user: "User" = Relationship(back_populates="mechanic_profile")
    trips: List["Trip"] = Relationship(back_populates="mechanic")
    offers: List["MechanicOffer"] = Relationship(back_populates="mechanic")


class MechanicOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    mechanic_id: int = Field(foreign_key="mechanic.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

    trip: "Trip" = Relationship(back_populates="mechanic_offers")
    mechanic: Mechanic = Relationship(back_populates="offers")


class MechanicUpdate(SQLModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    specialization: Optional[str] = None
    address: Optional[str] = None
    status: Optional[str] = None


class MechanicPublic(SQLModel):
    id: int
    name: str
    specialization: str
    status: str
    rating: float
    profile_picture_url: Optional[str] = None
    total_trips: Optional[int] = 0


class MechanicPrivate(MechanicPublic):
    phone_number: str
    address: Optional[str] = None
    user_id: uuid.UUID


class MechanicReviewBase(SQLModel):
    rating: float
    comment: Optional[str] = None


class MechanicReview(MechanicReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    mechanic_id: int = Field(foreign_key="mechanic.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --- SERVICE BOOKING TYPE AND STATUS ENUMS ---
class BookingType(str, Enum):
    """Enum for service booking types"""

    SLOT_BASED = "slot_based"
    WALK_IN = "walk_in"


class ServiceStatus(str, Enum):
    """Enum for service request status"""

    # Shared statuses
    SEARCHING = "searching"
    CANCELLED = "cancelled"
    COMPLETED = "completed"

    # Slot-based specific
    BOOKED = "booked"
    CHECKED_IN = "checked_in"

    # Walk-in specific
    ACCEPTED = "accepted"
    SERVICE_ONGOING = "service_ongoing"
    SERVICE_ACCEPTED = "service_accepted"


# --- SERVICE CENTER MODELS ---


class ServiceCenterBase(SQLModel):
    name: str
    phone_number: str
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    profile_picture_url: Optional[str] = None
    status: str = "pending_approval"
    rating: float = Field(default=0.0)
    verification_documents: List[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    admin_notes: Optional[str] = None


class ServiceCenter(ServiceCenterBase, table=True):
    """Service center/garage profile (e.g., for vehicle service, PPF, wash, etc.)"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: "User" = Relationship(back_populates="service_center_profile")
    services: List["CenterService"] = Relationship(back_populates="service_center")
    slots: List["ServiceSlot"] = Relationship(back_populates="service_center")
    bookings: List["ServiceRequest"] = Relationship(back_populates="service_center")
    reviews: List["ServiceCenterReview"] = Relationship(back_populates="service_center")


class CenterService(SQLModel, table=True):
    """Custom Service type offered by a service center"""

    id: Optional[int] = Field(default=None, primary_key=True)
    service_center_id: int = Field(foreign_key="servicecenter.id")

    service_name: str
    booking_type: BookingType = BookingType.SLOT_BASED

    vehicle_types: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    is_walk_in_allowed: bool = False

    max_daily_bookings: int = 5
    service_duration_hours: float = 2.0
    slot_start_time: str = "10:00"
    slot_end_time: str = "19:00"
    slot_interval_minutes: int = 30

    allow_overlapping_bookings: bool = False
    max_concurrent_bookings: int = 1

    pricing_components: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    price: Optional[float] = None
    description: Optional[str] = None
    is_available: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)

    service_center: ServiceCenter = Relationship(back_populates="services")
    slots: List["ServiceSlot"] = Relationship(back_populates="center_service")
    bookings: List["ServiceRequest"] = Relationship(back_populates="center_service")


class PricingComponent(SQLModel, table=True):
    """Pricing component breakdown for services and bookings (for transparency)"""

    id: Optional[int] = Field(default=None, primary_key=True)
    service_id: Optional[int] = Field(default=None, foreign_key="centerservice.id")
    booking_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id")

    component_name: str  # "Labor", "Material", "Tax", "Installation", etc.
    amount: float  # Amount for this component
    percentage: Optional[float] = None  # Percentage of total
    description: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class ServiceSlot(SQLModel, table=True):
    """Time slots for slot-based services with overlapping support"""

    id: Optional[int] = Field(default=None, primary_key=True)
    service_center_id: int = Field(foreign_key="servicecenter.id")
    center_service_id: int = Field(foreign_key="centerservice.id")
    start_time: datetime
    end_time: datetime

    # Capacity management
    max_capacity: int = 1  # Maximum concurrent bookings allowed
    booked_count: int = 0  # Current bookings in this slot
    is_available: bool = True

    service_center: ServiceCenter = Relationship(back_populates="slots")
    center_service: CenterService = Relationship(back_populates="slots")
    bookings: List["ServiceRequest"] = Relationship(back_populates="slot")


class ServiceRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    service_center_id: int = Field(foreign_key="servicecenter.id")
    center_service_id: int = Field(foreign_key="centerservice.id")

    booking_type: BookingType = BookingType.SLOT_BASED
    service_name: str  # REPLACED service_type
    vehicle_type: str
    vehicle_number: Optional[str] = None
    slot_id: Optional[int] = Field(default=None, foreign_key="serviceslot.id")

    status: ServiceStatus = ServiceStatus.SEARCHING
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None
    expected_return_date: Optional[date] = None
    expected_return_time: Optional[str] = None
    actual_return_date: Optional[date] = None
    actual_return_time: Optional[str] = None

    booking_time: datetime = Field(default_factory=datetime.utcnow)
    checked_in_time: Optional[datetime] = None
    service_accepted_time: Optional[datetime] = None
    completed_time: Optional[datetime] = None

    price_at_booking: Optional[float] = None
    final_price: Optional[float] = None
    price_locked: bool = False
    price_components: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )

    user: "User" = Relationship(back_populates="service_bookings")
    service_center: ServiceCenter = Relationship(back_populates="bookings")
    center_service: CenterService = Relationship(back_populates="bookings")
    slot: Optional[ServiceSlot] = Relationship(back_populates="bookings")


class ServiceCenterReviewBase(SQLModel):
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class ServiceCenterReview(ServiceCenterReviewBase, table=True):
    """Review for service center"""

    id: Optional[int] = Field(default=None, primary_key=True)
    service_center_id: int = Field(foreign_key="servicecenter.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    service_request_id: Optional[int] = Field(foreign_key="servicerequest.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    service_center: ServiceCenter = Relationship(back_populates="reviews")


# --- API Response Models for Service Center ---
class ServiceCenterPublic(SQLModel):
    id: int
    name: str
    address: str
    latitude: float
    longitude: float
    profile_picture_url: Optional[str] = None
    status: str
    rating: float
    total_bookings: Optional[int] = 0
    distance: Optional[float] = None


class ServiceCenterPrivate(ServiceCenterBase):
    id: int
    user_id: uuid.UUID
    created_at: datetime


class ServiceCenterUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    profile_picture_url: Optional[str] = None
    status: Optional[str] = None


# --- API Models for Services ---
class CenterServicePublic(SQLModel):
    id: int
    service_center_id: int
    service_name: str
    booking_type: BookingType
    vehicle_types: List[str]
    is_walk_in_allowed: bool
    price: Optional[float] = None
    description: Optional[str] = None
    is_available: bool


class CenterServiceCreate(SQLModel):
    service_name: str
    booking_type: BookingType = BookingType.SLOT_BASED
    vehicle_types: List[str]
    is_walk_in_allowed: bool = False
    max_daily_bookings: int = 5
    service_duration_hours: float = 2.0
    slot_start_time: str = "10:00"
    slot_end_time: str = "19:00"
    slot_interval_minutes: int = 30
    allow_overlapping_bookings: bool = False
    max_concurrent_bookings: int = 1
    pricing_components: List[Dict[str, Any]] = Field(default_factory=list)
    price: Optional[float] = None
    description: Optional[str] = None
    is_available: bool = True


class CenterServiceUpdate(SQLModel):
    service_name: Optional[str] = None
    vehicle_types: Optional[List[str]] = None
    is_walk_in_allowed: Optional[bool] = None
    max_daily_bookings: Optional[int] = None
    service_duration_hours: Optional[float] = None
    slot_start_time: Optional[str] = None
    slot_end_time: Optional[str] = None
    allow_overlapping_bookings: Optional[bool] = None
    max_concurrent_bookings: Optional[int] = None
    pricing_components: Optional[List[Dict[str, Any]]] = None
    price: Optional[float] = None
    description: Optional[str] = None
    is_available: Optional[bool] = None


# --- API Models for Slots ---
class ServiceSlotPublic(SQLModel):
    id: int
    start_time: datetime
    end_time: datetime
    max_capacity: int
    booked_count: int
    is_available: bool


class ServiceSlotCreate(SQLModel):
    start_time: datetime
    end_time: datetime
    max_capacity: int = 1
    is_available: bool = True


# --- API Models for Service Requests ---
class ServiceRequestBase(SQLModel):
    vehicle_type: str
    vehicle_number: Optional[str] = None
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None


class ServiceRequestCreate(SQLModel):
    service_center_id: int
    center_service_id: int
    booking_type: BookingType = BookingType.SLOT_BASED
    vehicle_type: str
    vehicle_number: Optional[str] = None
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None
    slot_id: Optional[int] = None


class ServiceRequestPublic(SQLModel):
    id: int
    user_id: uuid.UUID
    service_center_id: int
    center_service_id: int
    booking_type: BookingType
    service_name: str
    vehicle_type: str
    vehicle_number: Optional[str] = None
    status: ServiceStatus
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None
    expected_return_date: Optional[date] = None
    expected_return_time: Optional[str] = None
    booking_time: datetime
    price_at_booking: Optional[float] = None
    final_price: Optional[float] = None
    price_components: List[Dict[str, Any]]


class ServiceRequestPrivate(ServiceRequestPublic):
    slot_id: Optional[int] = None
    checked_in_time: Optional[datetime] = None
    service_accepted_time: Optional[datetime] = None
    actual_return_date: Optional[date] = None
    actual_return_time: Optional[str] = None
    price_locked: bool = False


class ServiceRequestForCenter(ServiceRequestPublic):
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None


class ServiceRequestUpdate(SQLModel):
    status: Optional[ServiceStatus] = None
    expected_return_date: Optional[date] = None
    expected_return_time: Optional[str] = None
    final_price: Optional[float] = None
    price_components: Optional[List[Dict[str, Any]]] = None


class ServiceSlotUpdate(SQLModel):
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    max_capacity: Optional[int] = None
    is_available: Optional[bool] = None


# --- Trip Models ---
class TripBase(SQLModel):
    user_id: uuid.UUID = Field(foreign_key="user.id")
    driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")
    tow_truck_driver_id: Optional[int] = Field(
        default=None, foreign_key="towtruckdriver.id"
    )

    # Booking Details
    hiring_type: str
    vehicle_type: str
    shift_details: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    months: Optional[int] = None
    selected_days: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    reason: Optional[str] = None
    fare: Optional[float] = None
    status: str = "searching"
    booking_time: datetime = Field(default_factory=datetime.utcnow)


class Trip(TripBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver: Optional["Driver"] = Relationship(back_populates="trips")
    tow_truck_driver: Optional["TowTruckDriver"] = Relationship(
        back_populates="trips"
    )  # Added
    user: "User" = Relationship(back_populates="trips")
    offers: List["TripOffer"] = Relationship(back_populates="trip")
    tow_offers: List["TowTripOffer"] = Relationship(back_populates="trip")
    mechanic_id: Optional[int] = Field(default=None, foreign_key="mechanic.id")
    mechanic: Optional["Mechanic"] = Relationship(back_populates="trips")
    mechanic_offers: List["MechanicOffer"] = Relationship(back_populates="trip")


class TripOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    driver_id: int = Field(foreign_key="driver.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

    trip: Trip = Relationship(back_populates="offers")
    driver: "Driver" = Relationship(back_populates="offers")


class TowTripOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    tow_truck_driver_id: int = Field(foreign_key="towtruckdriver.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

    trip: Trip = Relationship(back_populates="tow_offers")
    driver: "TowTruckDriver" = Relationship(back_populates="offers")


# --- SAFETY LAYER: RESPONSE MODELS ---
class TripSafe(SQLModel):
    id: int
    hiring_type: str
    vehicle_type: str
    shift_details: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    months: Optional[int] = None
    selected_days: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    reason: Optional[str] = None
    status: str
    fare: Optional[float] = None
    booking_time: datetime


class TripOfferPublic(SQLModel):
    id: int
    status: str
    tier: int
    created_at: datetime
    trip: TripSafe


# --- Table Models ---
class Driver(DriverBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    rating: float = Field(default=0.0)

    user: "User" = Relationship(back_populates="driver_profile")
    trips: List[Trip] = Relationship(back_populates="driver")
    reviews: List["DriverReview"] = Relationship(back_populates="driver")
    offers: List[TripOffer] = Relationship(back_populates="driver")


class TowTruckDriver(TowTruckDriverBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")

    user: "User" = Relationship(back_populates="tow_truck_driver_profile")
    trips: List[Trip] = Relationship(back_populates="tow_truck_driver")
    reviews: List["TowTruckDriverReview"] = Relationship(back_populates="driver")
    offers: List[TowTripOffer] = Relationship(back_populates="driver")


# --- API Response Models ---
class DriverPublic(SQLModel):
    id: int
    name: str
    rating: float
    profile_picture_url: Optional[str] = None
    years_of_experience: Optional[int]
    vehicle_type: Optional[str]
    spoken_languages: Optional[str]
    status: str
    total_trips: int = 0


class TowTruckDriverPublic(SQLModel):
    id: int
    name: str
    rating: float
    profile_picture_url: Optional[str] = None
    vehicle_number: str
    status: str
    total_trips: int = 0


class TripReadUser(TripSafe):
    driver: Optional[DriverPublic] = None
    tow_truck_driver: Optional[TowTruckDriverPublic] = (
        None  # Added support for tow driver details
    )
    mechanic: Optional[MechanicPublic] = None


class DriverPrivate(DriverBase):
    id: int
    rating: float


class TowTruckDriverPrivate(TowTruckDriverBase):
    id: int


# --- Update Models ---
class DriverUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone_number: Optional[str] = None
    emergency_phone: Optional[str] = None
    profile_picture_url: Optional[str] = None
    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None
    fare_per_km: Optional[float] = None
    driver_allowance: Optional[float] = None
    spoken_languages: Optional[str] = None
    status: Optional[str] = None


class TowTruckDriverUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone_number: Optional[str] = None
    vehicle_number: Optional[str] = None
    profile_picture_url: Optional[str] = None
    status: Optional[str] = None


# --- Review Models ---
class DriverReviewBase(SQLModel):
    user_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class DriverReview(DriverReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_id: int = Field(foreign_key="driver.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    driver: "Driver" = Relationship(back_populates="reviews")


class TowTruckDriverReview(DriverReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_id: int = Field(foreign_key="towtruckdriver.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    driver: "TowTruckDriver" = Relationship(back_populates="reviews")


# --- User Models ---
class UserBase(SQLModel):
    phone_number: str = Field(index=True)
    email: Optional[EmailStr] = Field(default=None, unique=True, index=True)
    full_name: Optional[str] = None
    provider: str = "local"
    avatar_url: Optional[str] = None
    role: str = "user"


class UserDevice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    token: str = Field(index=True)  # The Expo Push Token
    platform: Optional[str] = None  # 'ios' or 'android'
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    user: "User" = Relationship(back_populates="devices")


# --- NEW: SYSTEM CONFIGURATION ---
class SystemConfig(SQLModel, table=True):
    key: str = Field(primary_key=True)  # e.g., "bike_base_fare", "car_per_km"
    value: str  # We store as string and cast later (e.g., "450.0")
    description: Optional[str] = None


# --- NEW: SUPPORT TICKET SYSTEM ---
class SupportTicketBase(SQLModel):
    subject: str = Field(max_length=150)
    description: str = Field(max_length=2000)
    category: str = "general"
    priority: str = "medium"

    # --- XSS Sanitization ---
    @field_validator("subject", "description", mode="before")
    def sanitize_html(cls, v):
        if isinstance(v, str):
            # Escapes < to &lt;, > to &gt;, etc.
            return html.escape(v.strip())
        return v


class SupportTicket(SupportTicketBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    ticket_id: str = Field(index=True)  # Unique ID like "TKT-1001" for display
    status: str = "open"  # open, in_progress, resolved, closed
    admin_response: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    user: "User" = Relationship(back_populates="tickets")


class SupportTicketCreate(SupportTicketBase):
    pass


class SupportTicketResponse(SupportTicketBase):
    id: int
    ticket_id: str
    status: str
    admin_response: Optional[str]
    created_at: datetime


class User(UserBase, table=True):
    __table_args__ = (UniqueConstraint("phone_number", "role", name="uix_phone_role"),)
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    hashed_password: Optional[str] = None
    force_password_change: bool = Field(default=False)
    devices: List["UserDevice"] = Relationship(back_populates="user")
    driver_profile: Optional[Driver] = Relationship(back_populates="user")
    tow_truck_driver_profile: Optional[TowTruckDriver] = Relationship(
        back_populates="user"
    )
    trips: List[Trip] = Relationship(back_populates="user")
    tickets: List["SupportTicket"] = Relationship(back_populates="user")
    mechanic_profile: Optional[Mechanic] = Relationship(back_populates="user")
    service_center_profile: Optional["ServiceCenter"] = Relationship(
        back_populates="user"
    )
    service_bookings: List["ServiceRequest"] = Relationship(back_populates="user")


class UserPublic(SQLModel):
    id: uuid.UUID
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    force_password_change: bool = False


class UserPrivate(UserBase):
    id: uuid.UUID


class UserUpdate(SQLModel):
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None


class UserCreate(SQLModel):
    email: Optional[EmailStr] = None
    password: str
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: str = "user"
    # Optional fields for Driver/Tow creation
    license_number: Optional[str] = None
    vehicle_type: Optional[str] = None
    phone_number: Optional[str] = None
    vehicle_number: Optional[str] = None  # Added for TowTruckDriver
    org_name: Optional[str] = None
    contact_number: Optional[str] = None
    address: Optional[str] = None

    @field_validator("full_name", "org_name", "address", mode="before")
    def sanitize_strings(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class UserLogin(SQLModel):
    email: Optional[EmailStr] = None
    password: str
    role: str


class Token(SQLModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: dict


# --- Trip API Models ---
class TripUpdate(SQLModel):
    status: Optional[str] = None


class TripCreate(TripBase):
    user_id: Optional[uuid.UUID] = None
    driver_id: Optional[int] = None
    tow_truck_driver_id: Optional[int] = None


class LocationUpdate(SQLModel):
    latitude: float
    longitude: float
    heading: Optional[float] = 0.0
    speed: Optional[float] = 0.0
    trip_id: Optional[int] = None


# Used for Send OTP API
class SendOTPRequest(SQLModel):
    phone_number: str
    role: str = "user"


# Used for Verify OTP API
class VerifyOTPRequest(SQLModel):
    phone_number: str
    otp: str
    role: str = (
        "user"  # "user", "driver", "tow_truck_driver", "mechanic", "service_center"
    )
    # Base user fields
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    # Driver Specific Fields
    license_number: Optional[str] = None
    vehicle_type: Optional[str] = None
    # Tow Truck Specific Fields
    vehicle_number: Optional[str] = None
    specialization: Optional[str] = None
    # Service Center Specific Fields
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# --- UI CONFIGURATION MODELS ---
class UIThemeBase(SQLModel):
    name: str  # e.g., "Ugadi", "Monsoon", "Deepavali"
    theme_type: str  # "FESTIVAL" or "SEASON"
    animation_style: str  # "SNOW", "RAIN", "FLOWERS", "DIYAS", "KITE", "NONE"
    start_date: Optional[datetime] = None  # Used for time-limited festivals
    end_date: Optional[datetime] = None
    is_active: bool = True


class UITheme(UIThemeBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)


class UIBannerBase(SQLModel):
    image_url: str
    title: str
    details_text: str  # Content for the expanded modal
    action_route: Optional[str] = None  # e.g., "/offers/deepavali"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    is_active: bool = True


class UIBanner(UIBannerBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
