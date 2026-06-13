import uuid
import html
from enum import Enum
from pydantic import EmailStr, field_validator, AliasChoices
from sqlmodel import Field, SQLModel, Relationship
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta, timezone
from sqlalchemy import UniqueConstraint, JSON, Column

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist_naive() -> datetime:
    """Current wall-clock time in IST as a naive datetime.

    Used as default_factory for trip-related models (Trip.booking_time,
    OTPRegistry, TripBill, TripAttendance, TripSettlement,
    PricingComponentBreakdown, TripOffer). Other models are unaffected.
    """
    return datetime.now(_IST).replace(tzinfo=None)


# --- Base Models (Shared fields) ---
def _coerce_none_to_empty_list(v):
    # Old DB rows may have NULL in `verification_documents` (the JSON column
    # was nullable before default_factory=list was added). Without this
    # coercion, response validation 500s on those rows.
    return [] if v is None else v


# Driver / tow-driver KYC documents are labelled one-per-requirement (no
# duplicates). Each key holds a single document URL.
PROVIDER_DOCUMENT_TYPES = ("aadhar", "pan", "license", "rc", "puc", "insurance")


def _coerce_none_to_empty_dict(v):
    # `verification_documents` moved from List[str] -> Dict[str, str] (keyed by
    # document type). Old rows may hold NULL or a legacy list; coerce both to a
    # dict so response validation never 500s on historical data.
    if v is None:
        return {}
    if isinstance(v, list):
        # Legacy positional list: map onto the fixed requirement order, best
        # effort. Unmapped extras are dropped (admin re-uploads to fix labels).
        return {
            PROVIDER_DOCUMENT_TYPES[i]: url
            for i, url in enumerate(v)
            if i < len(PROVIDER_DOCUMENT_TYPES) and url
        }
    return v


class DriverBase(SQLModel):
    name: str
    phone_number: str
    address: Optional[str] = None
    emergency_phone: Optional[str] = None
    profile_picture_url: Optional[str] = None
    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None
    vehicle_number: Optional[str] = None
    spoken_languages: Optional[str] = None
    status: str = "pending_approval"
    suspended_until: Optional[datetime] = None
    # Bank payout details (mandatory for approval; used by cash-out / payout).
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_account_holder_name: Optional[str] = None
    passbook_document_url: Optional[str] = None
    # Labelled KYC documents: {doc_type: url}. One document per requirement;
    # re-uploading a type replaces the previous URL. See PROVIDER_DOCUMENT_TYPES.
    verification_documents: Dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    admin_notes: Optional[str] = None

    _coerce_verification_documents = field_validator(
        "verification_documents", mode="before"
    )(lambda cls, v: _coerce_none_to_empty_dict(v))


class TowTruckDriverBase(SQLModel):
    name: str
    phone_number: str
    vehicle_number: Optional[str] = None
    service_type: str = "tow"
    tow_vehicle_type: Optional[str] = None
    transport_vehicle_type: Optional[str] = None
    address: Optional[str] = None
    profile_picture_url: Optional[str] = None
    status: str = "pending_approval"
    rating: float = Field(default=0.0)
    # Bank payout details (mandatory for approval; used by cash-out / payout).
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_account_holder_name: Optional[str] = None
    passbook_document_url: Optional[str] = None
    # Labelled KYC documents: {doc_type: url}. See PROVIDER_DOCUMENT_TYPES.
    verification_documents: Dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    admin_notes: Optional[str] = None

    _coerce_verification_documents = field_validator(
        "verification_documents", mode="before"
    )(lambda cls, v: _coerce_none_to_empty_dict(v))


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

    _coerce_verification_documents = field_validator(
        "verification_documents", mode="before"
    )(lambda cls, v: _coerce_none_to_empty_list(v))


class Mechanic(MechanicBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    current_lat: Optional[float] = Field(default=None)
    current_lng: Optional[float] = Field(default=None)
    location_updated_at: Optional[datetime] = Field(default=None)
    is_online: bool = Field(default=True)

    user: "User" = Relationship(back_populates="mechanic_profile")
    mechanic_trips: List["MechanicTrip"] = Relationship(back_populates="mechanic")
    offers: List["MechanicOffer"] = Relationship(back_populates="mechanic")


class MechanicOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="mechanictrip.id")
    mechanic_id: int = Field(foreign_key="mechanic.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

    trip: "MechanicTrip" = Relationship(back_populates="offers")
    mechanic: Mechanic = Relationship(back_populates="offers")


class MechanicUpdate(SQLModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    specialization: Optional[str] = None
    address: Optional[str] = None
    status: Optional[str] = None


class MechanicPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    name: str
    # Optional: the DB column allows NULL (MechanicBase.specialization), so the
    # public response must too — otherwise serializing a mechanic without one
    # set raises 500.
    specialization: Optional[str] = None
    status: str
    rating: float
    profile_picture_url: Optional[str] = None
    total_trips: Optional[int] = 0


class MechanicPrivate(MechanicPublic):
    phone_number: str
    address: Optional[str] = None
    is_online: bool = True


class MechanicReviewBase(SQLModel):
    rating: float
    comment: Optional[str] = None


class MechanicReview(MechanicReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    mechanic_id: int = Field(foreign_key="mechanic.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    # Booking this review is for — lets a customer rate the same mechanic on
    # different jobs (per-booking dedup), parity with ServiceCenterReview.
    mechanic_trip_id: Optional[int] = Field(
        default=None, foreign_key="mechanictrip.id"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --- SERVICE BOOKING TYPE AND STATUS ENUMS ---
class BookingType(str, Enum):
    """Enum for service booking types"""

    SLOT_BASED = "slot_based"
    WALK_IN = "walk_in"


class ServiceStatus(str, Enum):
    SEARCHING = "searching"  # legacy/unused
    PENDING_CONFIRMATION = (
        "pending_confirmation"  # slot over-capacity: awaits center accept/decline
    )
    BOOKED = "booked"
    ACCEPTED = "accepted"
    CHECKED_IN = "checked_in"
    SERVICE_ONGOING = "service_ongoing"
    SERVICE_ACCEPTED = "service_accepted"
    IN_SERVICE = "in_service"  # legacy/unused (replaced by service_ongoing)
    COMPLETED = "completed"
    CANCELLED = "cancelled"


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

    _coerce_verification_documents = field_validator(
        "verification_documents", mode="before"
    )(lambda cls, v: _coerce_none_to_empty_list(v))


class ServiceCenter(ServiceCenterBase, table=True):
    """Service center/garage profile (e.g., for vehicle service, PPF, wash, etc.)"""

    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    # Short, human-shareable 6-char code (A-Z+0-9) members type at signup to
    # register under this center. Distinct from the sequential reference_id.
    center_code: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: "User" = Relationship(back_populates="service_center_profile")
    services: List["CenterService"] = Relationship(back_populates="service_center")
    slots: List["ServiceSlot"] = Relationship(back_populates="service_center")
    bookings: List["ServiceRequest"] = Relationship(back_populates="service_center")
    reviews: List["ServiceCenterReview"] = Relationship(back_populates="service_center")
    center_members: List["CenterMember"] = Relationship(back_populates="service_center")


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
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    service_center_id: int = Field(foreign_key="servicecenter.id")
    center_service_id: int = Field(foreign_key="centerservice.id")

    booking_type: BookingType = BookingType.SLOT_BASED
    service_name: str
    vehicle_type: str
    vehicle_number: Optional[str] = None
    vehicle_model: Optional[str] = None
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
    cancellation_time: Optional[datetime] = None

    price_at_booking: Optional[float] = None
    final_price: Optional[float] = None
    price_locked: bool = False
    payment_status: str = "unpaid"  # synced by the centralized payment module
    advance_amount: Optional[float] = None
    amount_paid: float = 0.0
    price_components: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    cancellation_reason: Optional[str] = None

    # Center-member assignment (manual by center or system auto-assign). The
    # assigned member is NEVER exposed to the end user.
    assigned_member_id: Optional[int] = Field(
        default=None, foreign_key="centermember.id"
    )
    assigned_at: Optional[datetime] = None
    auto_assigned: bool = Field(default=False)

    user: "User" = Relationship(back_populates="service_bookings")
    service_center: ServiceCenter = Relationship(back_populates="bookings")
    center_service: CenterService = Relationship(back_populates="bookings")
    slot: Optional[ServiceSlot] = Relationship(back_populates="bookings")
    assigned_member: Optional["CenterMember"] = Relationship(
        back_populates="assignments"
    )


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


# --- CENTER MEMBER MODELS ---


class CenterMember(SQLModel, table=True):
    """A worker/technician who belongs to a service center and is assigned to its
    bookings. Signs up in the driver app with a center_code and is approved by
    the center (no admin approval). Has no wallet/help and is read-only on
    bookings. The center drives all booking status changes."""

    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    service_center_id: int = Field(foreign_key="servicecenter.id")

    name: str
    phone_number: str
    profile_picture_url: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    # Skills the member can handle. Each value MUST be one of the parent
    # center's CenterService.service_name (validated at signup / profile-update).
    expert_in: List[str] = Field(default_factory=list, sa_column=Column(JSON))

    # pending_approval | approved | rejected | suspended | banned
    status: str = "pending_approval"
    is_online: bool = Field(default=True)  # member's own availability toggle
    rating: float = Field(default=0.0)
    total_reviews: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    user: "User" = Relationship(back_populates="center_member_profile")
    service_center: "ServiceCenter" = Relationship(back_populates="center_members")
    reviews: List["CenterMemberReview"] = Relationship(back_populates="member")
    assignments: List["ServiceRequest"] = Relationship(back_populates="assigned_member")

    _validate_gender = field_validator("gender", mode="before")(
        lambda cls, v: _normalize_gender(v)
    )
    _coerce_expert_in = field_validator("expert_in", mode="before")(
        lambda cls, v: _coerce_none_to_empty_list(v)
    )


class CenterMemberReviewBase(SQLModel):
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class CenterMemberReview(CenterMemberReviewBase, table=True):
    """Customer's review of the member who handled their booking. The member is
    derived from ServiceRequest.assigned_member_id and stays anonymous to the
    user — only the aggregate (rating / total_reviews) is ever exposed."""

    id: Optional[int] = Field(default=None, primary_key=True)
    center_member_id: int = Field(foreign_key="centermember.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    service_request_id: Optional[int] = Field(
        default=None, foreign_key="servicerequest.id"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)

    member: "CenterMember" = Relationship(back_populates="reviews")


# --- UNIFIED APP/SERVICE RATING (all 4 services) ---
class ServiceRatingBase(SQLModel):
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class ServiceRating(ServiceRatingBase, table=True):
    """App/service-level rating, unified across all 4 services (trip, tow,
    transport, mechanic, service-center). One row per (service_type, booking)
    per user. Provider ratings live in their own per-provider review tables and
    aggregate into the provider profile ``.rating`` columns; THIS table is the
    app's own service score, so admin can see service satisfaction next to
    provider performance from a single place."""

    id: Optional[int] = Field(default=None, primary_key=True)
    service_type: str = Field(index=True)  # trip | tow | transport | mechanic | service_center
    booking_id: int = Field(index=True)  # internal PK of the booking row
    booking_reference_id: str = Field(index=True)  # TP.. / TW.. / MC.. / SB..
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --- API Response Models for Service Center ---
class ServiceCenterPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
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
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    center_code: Optional[str] = None
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
    vehicle_model: Optional[str] = None
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None


class ServiceRequestCreate(SQLModel):
    service_center_id: str  # ServiceCenter.reference_id (e.g. SC20260001)
    center_service_id: int
    booking_type: BookingType = BookingType.SLOT_BASED
    vehicle_type: str
    vehicle_number: Optional[str] = None
    vehicle_model: Optional[str] = None
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None
    slot_id: Optional[int] = None


class ServiceRequestPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    payment_status: Optional[str] = None
    booking_type: BookingType
    service_name: str
    vehicle_type: str
    vehicle_number: Optional[str] = None
    vehicle_model: Optional[str] = None
    status: ServiceStatus
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None
    expected_return_date: Optional[date] = None
    expected_return_time: Optional[str] = None
    booking_time: datetime
    price_at_booking: Optional[float] = None
    final_price: Optional[float] = None
    advance_amount: Optional[float] = None
    amount_paid: float = 0.0
    price_components: List[Dict[str, Any]]
    cancellation_reason: Optional[str] = None


class ServiceRequestPrivate(ServiceRequestPublic):
    slot_id: Optional[int] = None
    checked_in_time: Optional[datetime] = None
    service_accepted_time: Optional[datetime] = None
    actual_return_date: Optional[date] = None
    actual_return_time: Optional[str] = None
    price_locked: bool = False
    cancellation_time: Optional[datetime] = None


class ServiceRequestForCenter(ServiceRequestPublic):
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None


class ServiceRequestUpdate(SQLModel):
    status: Optional[ServiceStatus] = None
    expected_return_date: Optional[date] = None
    expected_return_time: Optional[str] = None
    final_price: Optional[float] = None
    price_components: Optional[List[Dict[str, Any]]] = None
    cancellation_reason: Optional[str] = None


class ServiceSlotUpdate(SQLModel):
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    max_capacity: Optional[int] = None
    is_available: Optional[bool] = None


# --- API Models for Center Members ---
class CenterMemberPublic(SQLModel):
    """Member's own /me view (driver app). No center_code / internal id."""

    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    name: str
    phone_number: str
    profile_picture_url: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    expert_in: List[str] = Field(default_factory=list)
    status: str
    is_online: bool = True
    rating: float = 0.0
    total_reviews: int = 0
    center_name: Optional[str] = None


class CenterMemberProfileUpdate(SQLModel):
    """Member self-service profile edit — ONLY these fields are editable.
    (Profile photo is handled by the dedicated upload endpoint.)"""

    name: Optional[str] = None
    date_of_birth: Optional[date] = None
    expert_in: Optional[List[str]] = None


class CenterMemberForCenter(SQLModel):
    """Center's view of a member (list + detail). No internal id / center_code /
    phone — `current_assignments` carries sanitized booking blocks only."""

    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    name: str
    gender: Optional[str] = None
    profile_picture_url: Optional[str] = None
    expert_in: List[str] = Field(default_factory=list)
    is_online: bool = True
    status: str
    rating: float = 0.0
    total_reviews: int = 0
    current_assignments: List[Dict[str, Any]] = Field(default_factory=list)
    # Detail-only stats (None in list view).
    total_completed: Optional[int] = None
    total_pending: Optional[int] = None
    total_hours_worked: Optional[float] = None


class CenterMemberAssignCandidate(SQLModel):
    """Row in the center's "assign a member" picker."""

    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    name: str
    expert_in: List[str] = Field(default_factory=list)
    is_online: bool = True
    active_task_count: int = 0
    soonest_free_at: Optional[datetime] = None
    expertise_match: bool = True


class MemberAssignmentSummary(SQLModel):
    """Member's sanitized view of an assigned booking — NO price / payment /
    customer data."""

    booking_id: str
    service_name: str
    booking_type: BookingType
    status: ServiceStatus
    vehicle_type: str
    vehicle_number: Optional[str] = None
    vehicle_model: Optional[str] = None
    requested_date: Optional[date] = None
    requested_time: Optional[str] = None
    expected_return_date: Optional[date] = None
    expected_return_time: Optional[str] = None
    actual_return_date: Optional[date] = None
    actual_return_time: Optional[str] = None
    booking_time: datetime
    assigned_at: Optional[datetime] = None


class MemberAssignmentRequest(SQLModel):
    """Body for the center assigning a member to a booking."""

    member_id: str  # CenterMember.reference_id


class MemberApprovalRequest(SQLModel):
    """Body for the center approving / rejecting / (de)activating a member."""

    action: str  # approve | reject | suspend | reactivate
    note: Optional[str] = None


# --- Trip Models ---
class TripBase(SQLModel):
    user_id: uuid.UUID = Field(foreign_key="user.id")
    driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")

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

    # Geographic info for outstation pricing.
    # State for permit calculation is derived from end_location text + (optional) end coords;
    # distance can be supplied directly or computed from start/end coords (Haversine).
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None

    # Fare: 'fare' is the computed total (kept as float for back-compat with payment helpers).
    # 'fare_breakdown' is the per-component split (JSON), populated by the pricing engine.
    fare: Optional[float] = None
    fare_breakdown: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )

    status: str = "searching"
    booking_time: datetime = Field(default_factory=_now_ist_naive)

    # NEW FIELDS FOR TRIP MANAGEMENT
    trip_duration_hours: Optional[int] = None
    payment_method: Optional[str] = None  # "trip_day", "advance_20", "full_payment"
    scheduled_start_time: Optional[datetime] = None
    scheduled_end_time: Optional[datetime] = None
    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None
    driver_payment_status: str = "unpaid"  # unpaid, paid, skipped
    driver_payment_amount: Optional[float] = None
    driver_payment_due_date: Optional[datetime] = None
    driver_accepted_at: Optional[datetime] = None  # When driver clicked accept
    state_version: int = Field(default=1)  # For optimistic locking

    # Pause flag: when True, OTP generation for the next shift is blocked until
    # user clears all outstanding bills (used by trip_day payment method).
    is_payment_blocked: bool = False


class Trip(TripBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    driver: Optional["Driver"] = Relationship(back_populates="trips")
    user: "User" = Relationship(back_populates="trips")
    offers: List["TripOffer"] = Relationship(back_populates="trip")


class TripOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    driver_id: int = Field(foreign_key="driver.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=_now_ist_naive)

    trip: Trip = Relationship(back_populates="offers")
    driver: "Driver" = Relationship(back_populates="offers")


class TowTripOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="towtrip.id")
    tow_truck_driver_id: int = Field(foreign_key="towtruckdriver.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

    trip: "TowTrip" = Relationship(back_populates="offers")
    driver: "TowTruckDriver" = Relationship(back_populates="offers")


# --- MECHANIC TRIP (dedicated table for "Mechanic Service" bookings) ---
class MechanicTripBase(SQLModel):
    user_id: uuid.UUID = Field(foreign_key="user.id")
    mechanic_id: Optional[int] = Field(default=None, foreign_key="mechanic.id")
    vehicle_type: str
    start_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    reason: Optional[str] = None
    fare: Optional[float] = None
    fare_breakdown: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    status: str = "searching"
    payment_status: str = "unpaid"  # synced by the centralized payment module
    booking_time: datetime = Field(default_factory=_now_ist_naive)
    state_version: int = Field(default=1)


class MechanicTrip(MechanicTripBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    actual_start_time: Optional[datetime] = Field(default=None)
    actual_end_time: Optional[datetime] = Field(default=None)
    payment_due_at: Optional[datetime] = Field(default=None)
    mechanic: Optional["Mechanic"] = Relationship(back_populates="mechanic_trips")
    user: "User" = Relationship(back_populates="mechanic_trips")
    offers: List["MechanicOffer"] = Relationship(back_populates="trip")


# --- TOW TRIP (dedicated table for "Tow Service" bookings) ---
class TowTripBase(SQLModel):
    user_id: uuid.UUID = Field(foreign_key="user.id")
    tow_truck_driver_id: Optional[int] = Field(
        default=None, foreign_key="towtruckdriver.id"
    )
    vehicle_type: str  # the customer's vehicle (BIKE/CAR/...)
    service_type: str = "tow"
    tow_vehicle_type: Optional[str] = None
    transport_vehicle_type: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
    reason: Optional[str] = None
    fare: Optional[float] = None
    fare_breakdown: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    status: str = "searching"
    payment_status: str = "unpaid"  # synced by the centralized payment module
    booking_time: datetime = Field(default_factory=_now_ist_naive)
    state_version: int = Field(default=1)


class TowTrip(TowTripBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    actual_start_time: Optional[datetime] = Field(default=None)
    actual_end_time: Optional[datetime] = Field(default=None)
    payment_due_at: Optional[datetime] = Field(default=None)
    tow_truck_driver: Optional["TowTruckDriver"] = Relationship(
        back_populates="tow_trips"
    )
    user: "User" = Relationship(back_populates="tow_trips")
    offers: List["TowTripOffer"] = Relationship(back_populates="trip")

    @property
    def requested_vehicle_class(self) -> Optional[str]:
        """The provider class requested for this booking, picked by service_type:
        ``transport_vehicle_type`` for transport bookings, else
        ``tow_vehicle_type``. Centralizes "which class did the user ask for"."""
        if (self.service_type or "tow") == "transport":
            return self.transport_vehicle_type
        return self.tow_vehicle_type


# --- SAFETY LAYER: RESPONSE MODELS ---
class TripSafe(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
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
    fare_breakdown: Optional[Dict[str, Any]] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
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
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")
    rating: float = Field(default=0.0)

    user: "User" = Relationship(back_populates="driver_profile")
    trips: List[Trip] = Relationship(back_populates="driver")
    reviews: List["DriverReview"] = Relationship(back_populates="driver")
    offers: List[TripOffer] = Relationship(back_populates="driver")


class TowTruckDriver(TowTruckDriverBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id")

    # See Mechanic.current_lat — same plain-float live-location scheme; Postgres
    # derives a generated GEOGRAPHY `current_location` + GiST index in migration.
    current_lat: Optional[float] = Field(default=None)
    current_lng: Optional[float] = Field(default=None)
    location_updated_at: Optional[datetime] = Field(default=None)
    is_online: bool = Field(default=True)

    user: "User" = Relationship(back_populates="tow_truck_driver_profile")
    tow_trips: List["TowTrip"] = Relationship(back_populates="tow_truck_driver")
    reviews: List["TowTruckDriverReview"] = Relationship(back_populates="driver")
    offers: List[TowTripOffer] = Relationship(back_populates="driver")

    @property
    def vehicle_class(self) -> Optional[str]:
        """The class this provider is registered for, picked by service_type:
        ``transport_vehicle_type`` for transport providers, else
        ``tow_vehicle_type``."""
        if (self.service_type or "tow") == "transport":
            return self.transport_vehicle_type
        return self.tow_vehicle_type


class BookingOTP(SQLModel, table=True):
    """Geofenced start/end OTP for tow & mechanic bookings.

    The telemetry worker generates an OTP once the provider reaches the pickup
    geofence; the provider enters it manually to START a tow (or END a mechanic
    job). Independent of the regular-trip OTPRegistry, which is keyed on
    TripAttendance and has no equivalent for these per-table bookings.

    Invariant: at most one *active* (unverified, unexpired) row per
    (booking_type, booking_id); regeneration supersedes the previous row.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    booking_type: str  # "tow" | "mechanic"
    booking_id: int = Field(index=True)
    otp_hash: str  # sha256(otp)
    otp_plain: Optional[str] = Field(default=None)
    expires_at: datetime
    verified_at: Optional[datetime] = Field(default=None)
    verified_by_user_id: Optional[uuid.UUID] = Field(default=None)
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)
    created_at: datetime = Field(default_factory=_now_ist_naive)


# --- API Response Models ---
class DriverPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    name: str
    rating: float
    profile_picture_url: Optional[str] = None
    years_of_experience: Optional[int]
    vehicle_type: Optional[str]
    spoken_languages: Optional[str]
    status: str
    total_trips: int = 0


class TowTruckDriverPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    name: str
    rating: float
    profile_picture_url: Optional[str] = None
    vehicle_number: Optional[str] = None
    service_type: str = "tow"
    tow_vehicle_type: Optional[str] = None
    transport_vehicle_type: Optional[str] = None
    status: str
    total_trips: int = 0


class TripReadUser(TripSafe):
    driver: Optional[DriverPublic] = None
    tow_truck_driver: Optional[TowTruckDriverPublic] = None
    mechanic: Optional[MechanicPublic] = None


# --- MechanicTrip / TowTrip response models ---
class MechanicTripSafe(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    payment_status: Optional[str] = None
    hiring_type: str = "Mechanic Service"
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
    fare_breakdown: Optional[Dict[str, Any]] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
    booking_time: datetime


class MechanicTripReadUser(MechanicTripSafe):
    mechanic: Optional[MechanicPublic] = None


class MechanicOfferPublic(SQLModel):
    id: int
    status: str
    tier: int
    created_at: datetime
    trip: MechanicTripSafe


class TowTripSafe(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    payment_status: Optional[str] = None
    hiring_type: str = "Tow Service"
    vehicle_type: str
    service_type: str = "tow"
    tow_vehicle_type: Optional[str] = None
    transport_vehicle_type: Optional[str] = None
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
    fare_breakdown: Optional[Dict[str, Any]] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
    booking_time: datetime


class TowTripReadUser(TowTripSafe):
    tow_truck_driver: Optional[TowTruckDriverPublic] = None


class TowTripOfferPublic(SQLModel):
    id: int
    status: str
    tier: int
    created_at: datetime
    trip: TowTripSafe


class DriverPrivate(DriverBase):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    rating: float


class TowTruckDriverPrivate(TowTruckDriverBase):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    is_online: bool = True


class AvailabilityUpdate(SQLModel):
    """Body for the tow-driver / mechanic online-offline toggle."""

    is_online: bool


# --- Update Models ---
class DriverUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone_number: Optional[str] = None
    emergency_phone: Optional[str] = None
    profile_picture_url: Optional[str] = None
    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None
    vehicle_number: Optional[str] = None
    spoken_languages: Optional[str] = None
    status: Optional[str] = None


class TowTruckDriverUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone_number: Optional[str] = None
    vehicle_number: Optional[str] = None
    service_type: Optional[str] = None
    tow_vehicle_type: Optional[str] = None
    transport_vehicle_type: Optional[str] = None
    profile_picture_url: Optional[str] = None
    status: Optional[str] = None


class ProviderBankDetailsUpdate(SQLModel):
    """Driver / tow-driver bank payout details (set via /me/bank-details)."""

    bank_name: str
    bank_account_number: str
    bank_ifsc: str
    bank_account_holder_name: str


# --- Review Models ---
class DriverReviewBase(SQLModel):
    user_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class DriverReview(DriverReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_id: int = Field(foreign_key="driver.id")
    # Booking this review is for — enables per-booking dedup (a customer may
    # rehire and re-rate the same driver on a later trip).
    trip_id: Optional[int] = Field(default=None, foreign_key="trip.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    driver: "Driver" = Relationship(back_populates="reviews")


class TowTruckDriverReview(DriverReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_id: int = Field(foreign_key="towtruckdriver.id")
    # Booking this review is for — enables per-booking dedup.
    trip_id: Optional[int] = Field(default=None, foreign_key="towtrip.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    driver: "TowTruckDriver" = Relationship(back_populates="reviews")


# --- User Models ---
class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


def _normalize_gender(v):
    if v in (None, ""):
        return None
    v = str(v).strip().lower()
    allowed = {g.value for g in Gender}
    if v not in allowed:
        raise ValueError(f"gender must be one of {sorted(allowed)}")
    return v


class UserBase(SQLModel):
    phone_number: str = Field(index=True)
    email: Optional[EmailStr] = Field(default=None, unique=True, index=True)
    full_name: Optional[str] = None
    gender: Optional[str] = Field(
        default=None, description="One of: male | female | other | prefer_not_to_say"
    )
    provider: str = "local"
    avatar_url: Optional[str] = None
    role: str = "user"

    _validate_gender = field_validator("gender", mode="before")(
        lambda cls, v: _normalize_gender(v)
    )


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


class IdSequence(SQLModel, table=True):
    """Per-(entity_type, year) counter backing human-readable reference IDs.

    See app.utils.id_generator. Incremented under a row-level lock so the
    visible sequence stays gap-tight and monotonic within a calendar year.
    """

    entity_type: str = Field(primary_key=True)
    year: int = Field(primary_key=True)
    last_value: int = 0


# --- SUPPORT TICKET SYSTEM ---

# Allowed values (kept as plain strings in DB to match existing style)
SUPPORT_SERVICE_TYPES = {"trip", "tow", "mechanic", "service_center"}
SUPPORT_CATEGORIES = {
    "service_issue",
    "payment",
    "app_bug",
    "general",
    "other",
}
SUPPORT_RAISED_ROLES = {"user", "driver", "tow_truck_driver", "mechanic"}
SUPPORT_SENDER_ROLES = {
    "user",
    "driver",
    "tow_truck_driver",
    "mechanic",
    "admin",
    "system",
}


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
    admin_response: Optional[str] = (
        None  # legacy single-response field (kept for back-compat)
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Polymorphic service link (exactly-one-or-none)
    service_type: Optional[str] = Field(default=None, index=True)
    service_ref_id: Optional[int] = Field(default=None, index=True)
    service_snapshot: Optional[Dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )

    # Chat lifecycle
    last_message_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    closed_at: Optional[datetime] = None
    closed_by: Optional[str] = None  # "admin" | "auto_inactivity" | "user"
    auto_close_enabled: bool = Field(default=False)

    # Who raised the ticket (user app vs driver app)
    raised_by_role: str = Field(default="user")

    user: "User" = Relationship(back_populates="tickets")
    messages: List["SupportMessage"] = Relationship(
        back_populates="ticket",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    attachments: List["SupportAttachment"] = Relationship(
        back_populates="ticket",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class SupportTicketCreate(SupportTicketBase):
    service_type: Optional[str] = None
    service_ref_id: Optional[int] = None

    @field_validator("service_type", mode="before")
    def _validate_service_type(cls, v):
        if v is None or v == "":
            return None
        if v not in SUPPORT_SERVICE_TYPES:
            raise ValueError(
                f"service_type must be one of {sorted(SUPPORT_SERVICE_TYPES)}"
            )
        return v


class SupportTicketResponse(SupportTicketBase):
    id: int
    ticket_id: str
    status: str
    admin_response: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime] = None
    service_type: Optional[str] = None
    service_ref_id: Optional[int] = None
    last_message_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    closed_by: Optional[str] = None
    auto_close_enabled: bool = False
    raised_by_role: Optional[str] = None


# --- Support Messages (chat) ---
class SupportMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="supportticket.id", index=True)
    sender_role: str = Field(index=True)
    sender_id: str  # str(uuid) for customer, admin email for admin, "system" for system
    body: Optional[str] = Field(default=None, max_length=4000)
    is_system: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

    ticket: SupportTicket = Relationship(back_populates="messages")
    attachments: List["SupportAttachment"] = Relationship(back_populates="message")

    @field_validator("body", mode="before")
    def sanitize_body(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class SupportAttachment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="supportticket.id", index=True)
    message_id: Optional[int] = Field(
        default=None, foreign_key="supportmessage.id", index=True
    )
    file_url: str
    r2_key: str
    file_name: str = Field(max_length=255)
    file_type: str  # "image" | "document"
    mime_type: Optional[str] = None
    file_size: int = 0
    uploaded_by_role: str
    uploaded_by_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    ticket: SupportTicket = Relationship(back_populates="attachments")
    message: Optional[SupportMessage] = Relationship(back_populates="attachments")


# Response / request schemas for messages & attachments
class SupportAttachmentResponse(SQLModel):
    id: int
    ticket_id: int
    message_id: Optional[int] = None
    file_url: str
    file_name: str
    file_type: str
    mime_type: Optional[str] = None
    file_size: int
    uploaded_by_role: str
    created_at: datetime


class SupportMessageCreate(SQLModel):
    body: Optional[str] = Field(default=None, max_length=4000)
    attachment_ids: List[int] = Field(default_factory=list)

    @field_validator("body", mode="before")
    def sanitize_body(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class SupportMessageResponse(SQLModel):
    id: int
    ticket_id: int
    sender_role: str
    sender_id: str
    body: Optional[str] = None
    is_system: bool
    is_deleted: bool
    created_at: datetime
    attachments: List[SupportAttachmentResponse] = Field(default_factory=list)


class SupportTicketDetailResponse(SupportTicketResponse):
    service_snapshot: Optional[Dict[str, Any]] = None
    messages: List[SupportMessageResponse] = Field(default_factory=list)
    attachments: List[SupportAttachmentResponse] = Field(default_factory=list)


class SupportTicketStatusUpdate(SQLModel):
    status: str
    admin_response: Optional[str] = None

    @field_validator("status")
    def _check_status(cls, v):
        if v not in {"open", "in_progress", "resolved", "closed"}:
            raise ValueError("invalid status")
        return v


# --- Support FAQ (admin-managed pre-built solutions) ---
class SupportFAQ(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    service_type: Optional[str] = Field(default=None, index=True)
    category: str = Field(index=True)
    question: str = Field(max_length=200)
    solution: str = Field(max_length=4000)
    display_order: int = 0
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SupportFAQCreate(SQLModel):
    service_type: Optional[str] = None
    category: str = Field(max_length=80)
    question: str = Field(max_length=200)
    solution: str = Field(max_length=4000)
    display_order: int = 0
    is_active: bool = True

    @field_validator("service_type", mode="before")
    def _vt(cls, v):
        if v is None or v == "":
            return None
        if v not in SUPPORT_SERVICE_TYPES:
            raise ValueError(
                f"service_type must be one of {sorted(SUPPORT_SERVICE_TYPES)}"
            )
        return v


class SupportFAQUpdate(SQLModel):
    service_type: Optional[str] = None
    category: Optional[str] = None
    question: Optional[str] = None
    solution: Optional[str] = None
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


class SupportFAQResponse(SQLModel):
    id: int
    service_type: Optional[str]
    category: str
    question: str
    solution: str
    display_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


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
    mechanic_trips: List["MechanicTrip"] = Relationship(back_populates="user")
    tow_trips: List["TowTrip"] = Relationship(back_populates="user")
    tickets: List["SupportTicket"] = Relationship(back_populates="user")
    mechanic_profile: Optional[Mechanic] = Relationship(back_populates="user")
    service_center_profile: Optional["ServiceCenter"] = Relationship(
        back_populates="user"
    )
    center_member_profile: Optional["CenterMember"] = Relationship(
        back_populates="user"
    )
    service_bookings: List["ServiceRequest"] = Relationship(back_populates="user")
    addresses: List["UserAddress"] = Relationship(back_populates="user")
    saved_cards: List["SavedCard"] = Relationship(back_populates="user")
    wallet: Optional["Wallet"] = Relationship(back_populates="user")


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
    gender: Optional[str] = None

    _validate_gender = field_validator("gender", mode="before")(
        lambda cls, v: _normalize_gender(v)
    )


class UserCreate(SQLModel):
    email: Optional[EmailStr] = None
    password: str
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: str = "user"
    # Optional fields for Driver/Tow creation
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


# ======================================================================
# USER ACCOUNT: ADDRESSES / SAVED CARDS / WALLET
# ======================================================================


# --- Addresses ---
class AddressLabel(str, Enum):
    HOME = "home"
    WORK = "work"
    OTHER = "other"


class UserAddressBase(SQLModel):
    label: str = "home"  # home | work | other
    address_line: str = Field(max_length=500)  # full string address
    # {"lat": <float>, "lng": <float>} — coordinates stored as JSON per address.
    location: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    is_default: bool = False

    @field_validator("address_line", mode="before")
    def _sanitize_address(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class UserAddress(UserAddressBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    is_active: bool = Field(default=True, index=True)  # soft-delete flag
    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)

    user: "User" = Relationship(back_populates="addresses")


class UserAddressCreate(SQLModel):
    label: str = "home"
    address_line: str = Field(max_length=500)
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_default: bool = False

    @field_validator("label", mode="before")
    def _validate_label(cls, v):
        if v in (None, ""):
            return "home"
        if v not in {x.value for x in AddressLabel}:
            raise ValueError(f"label must be one of {[x.value for x in AddressLabel]}")
        return v

    @field_validator("lat")
    def _validate_lat(cls, v):
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("lat must be between -90 and 90")
        return v

    @field_validator("lng")
    def _validate_lng(cls, v):
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("lng must be between -180 and 180")
        return v

    @field_validator("address_line", mode="before")
    def _sanitize_address(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class UserAddressUpdate(SQLModel):
    label: Optional[str] = None
    address_line: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_default: Optional[bool] = None

    @field_validator("label", mode="before")
    def _validate_label(cls, v):
        if v is None:
            return v
        if v not in {x.value for x in AddressLabel}:
            raise ValueError(f"label must be one of {[x.value for x in AddressLabel]}")
        return v

    @field_validator("lat")
    def _validate_lat(cls, v):
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("lat must be between -90 and 90")
        return v

    @field_validator("lng")
    def _validate_lng(cls, v):
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("lng must be between -180 and 180")
        return v

    @field_validator("address_line", mode="before")
    def _sanitize_address(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class UserAddressPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    label: str
    address_line: str
    location: Dict[str, Any] = Field(default_factory=dict)
    is_default: bool
    created_at: datetime
    updated_at: datetime


# --- Saved Cards (tokenized — raw PAN/CVV are NEVER persisted) ---
class SavedCardBase(SQLModel):
    brand: str = "unknown"  # visa|mastercard|amex|rupay|diners|discover|unknown
    last4: str
    expiry_month: int
    expiry_year: int
    card_holder_name: Optional[str] = None
    nickname: Optional[str] = None
    is_default: bool = False


class SavedCard(SavedCardBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    # Gateway vault token + a fingerprint to dedupe the same physical card.
    # The raw card number / CVV are never stored here.
    card_token: str
    card_fingerprint: Optional[str] = Field(default=None, index=True)
    is_active: bool = Field(default=True, index=True)  # soft-delete flag
    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)

    user: "User" = Relationship(back_populates="saved_cards")


class SavedCardCreate(SQLModel):
    card_number: str  # raw PAN — tokenized then discarded, never stored/logged
    expiry_month: int
    expiry_year: int
    cvv: str  # used for tokenization only, never stored/logged
    card_holder_name: Optional[str] = None
    nickname: Optional[str] = None
    is_default: bool = False

    @field_validator("card_number", "cvv", mode="before")
    def _strip_sensitive(cls, v):
        if isinstance(v, str):
            return v.replace(" ", "").replace("-", "").strip()
        return v

    @field_validator("card_holder_name", "nickname", mode="before")
    def _sanitize(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class SavedCardUpdate(SQLModel):
    card_holder_name: Optional[str] = None
    nickname: Optional[str] = None
    is_default: Optional[bool] = None

    @field_validator("card_holder_name", "nickname", mode="before")
    def _sanitize(cls, v):
        if isinstance(v, str):
            return html.escape(v.strip())
        return v


class SavedCardPublic(SQLModel):
    """Safe card view — exposes brand/last4/expiry only, NEVER the token."""

    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    brand: str
    last4: str
    expiry_month: int
    expiry_year: int
    card_holder_name: Optional[str] = None
    nickname: Optional[str] = None
    is_default: bool
    created_at: datetime


# --- Wallet ---
class Wallet(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", unique=True, index=True)
    balance: float = 0.0
    currency: str = "INR"
    is_active: bool = True  # freeze/block flag
    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)

    user: "User" = Relationship(back_populates="wallet")


class WalletTransaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    wallet_id: int = Field(foreign_key="wallet.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)

    type: str  # "credit" | "debit"
    source: str  # topup | payment | refund | admin_credit | promo | reversal
    amount: float  # always positive
    balance_after: float = 0.0
    status: str = "success"  # pending | success | failed

    # Linkage / provenance
    payment_reference: Optional[str] = Field(default=None, index=True)
    related_service_type: Optional[str] = None
    related_service_reference_id: Optional[str] = None
    gateway_intent_id: Optional[str] = Field(default=None, index=True)
    idempotency_key: Optional[str] = Field(default=None, index=True)
    note: Optional[str] = None

    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)


class WalletPublic(SQLModel):
    balance: float
    currency: str
    is_active: bool
    updated_at: datetime


class WalletTransactionPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    type: str
    source: str
    amount: float
    balance_after: float
    status: str
    payment_reference: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime


class WalletTopupRequest(SQLModel):
    amount: float


class WalletAdminCreditRequest(SQLModel):
    user_reference: str  # target user's reference (phone_number)
    amount: float
    source: str = "admin_credit"  # admin_credit | promo
    note: Optional[str] = None

    @field_validator("source", mode="before")
    def _validate_source(cls, v):
        if v in (None, ""):
            return "admin_credit"
        if v not in {"admin_credit", "promo"}:
            raise ValueError("source must be 'admin_credit' or 'promo'")
        return v


# --- Provider Wallet (drivers / tow / mechanic / service-center) ------------
# Separate from the user `Wallet`: providers have a higher cap (₹1 lakh), can
# cash out to their bank, and may carry a NEGATIVE balance after an admin fraud
# deduction (recovered out of the next credit). Keyed by the provider's User id.
PROVIDER_TYPES = ("driver", "tow", "mechanic", "service_center")


class ProviderWallet(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", unique=True, index=True)
    provider_type: str = Field(index=True)  # driver | tow | mechanic | service_center
    balance: float = 0.0  # MAY go negative (fraud deduction recovery)
    currency: str = "INR"
    is_active: bool = True  # freeze/block flag
    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)


class ProviderWalletTransaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    wallet_id: int = Field(foreign_key="providerwallet.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)

    type: str  # "credit" | "debit"
    # online_payment | topup | cashout | payout | acceptance_fee |
    # admin_deduction | recovery | reversal
    source: str
    amount: float  # always positive; direction is in `type`
    balance_after: float = 0.0
    status: str = "success"  # pending | success | failed

    payment_reference: Optional[str] = Field(default=None, index=True)
    payout_reference: Optional[str] = Field(default=None, index=True)
    related_service_type: Optional[str] = None
    related_service_reference_id: Optional[str] = None
    gateway_intent_id: Optional[str] = Field(default=None, index=True)
    idempotency_key: Optional[str] = Field(default=None, index=True)
    note: Optional[str] = None

    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)


class ProviderWalletPublic(SQLModel):
    balance: float
    currency: str
    is_active: bool
    provider_type: str
    updated_at: datetime


class ProviderWalletTransactionPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    type: str
    source: str
    amount: float
    balance_after: float
    status: str
    payment_reference: Optional[str] = None
    payout_reference: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime


class ProviderTopupRequest(SQLModel):
    amount: float
    # card | upi — both settle to the platform merchant account via the gateway.
    method: str = "card"

    @field_validator("method", mode="before")
    def _validate_method(cls, v):
        if v in (None, ""):
            return "card"
        if v not in {"card", "upi"}:
            raise ValueError("method must be 'card' or 'upi'")
        return v


class ProviderCashoutRequest(SQLModel):
    amount: float


# --- Merchant bank ledger (single physical-money source of truth) -----------
# Every rupee that physically reaches the platform merchant bank account is one
# row here. `tag` segregates money owed to providers (`provider_owed`) from
# user wallet float (`user_float`) so the payout sweep never touches user money.
class MerchantBankLedger(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    entry_type: str = Field(index=True)  # credit | debit
    tag: str = Field(index=True)  # provider_owed | user_float | payout | cashout
    amount: float  # always positive; direction is in entry_type

    # Running balances maintained at append time (newest row = current state).
    running_balance: float = 0.0
    provider_owed_balance: float = 0.0
    user_float_balance: float = 0.0

    provider_user_id: Optional[uuid.UUID] = Field(default=None, index=True)
    provider_type: Optional[str] = None
    service_type: Optional[str] = None
    service_reference_id: Optional[str] = Field(default=None, index=True)
    payment_reference: Optional[str] = Field(default=None, index=True)
    payout_reference: Optional[str] = Field(default=None, index=True)
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_now_ist_naive)


class MerchantBankLedgerPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    entry_type: str
    tag: str
    amount: float
    running_balance: float
    provider_owed_balance: float
    user_float_balance: float
    provider_type: Optional[str] = None
    service_type: Optional[str] = None
    service_reference_id: Optional[str] = None
    payment_reference: Optional[str] = None
    payout_reference: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime


# --- Payout / cash-out records (single table; includes bounces) -------------
class PayoutRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)
    kind: str = Field(index=True)  # auto_payout | cashout
    # Not an FK: payout history is retained even if the provider account is
    # deleted (financial audit record).
    provider_user_id: uuid.UUID = Field(index=True)
    provider_type: str
    amount: float
    status: str = Field(
        default="initiated", index=True
    )  # initiated|success|bounced|failed

    bank_name: Optional[str] = None
    account_number_masked: Optional[str] = None
    ifsc: Optional[str] = None

    partner_provider: str = "mock"
    partner_txn_id: Optional[str] = None
    failure_reason: Optional[str] = None

    wallet_txn_reference: Optional[str] = Field(default=None, index=True)
    merchant_ledger_reference: Optional[str] = Field(default=None, index=True)
    idempotency_key: Optional[str] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)
    completed_at: Optional[datetime] = None


class PayoutRecordPublic(SQLModel):
    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    kind: str
    provider_type: str
    amount: float
    status: str
    bank_name: Optional[str] = None
    account_number_masked: Optional[str] = None
    ifsc: Optional[str] = None
    partner_provider: str
    partner_txn_id: Optional[str] = None
    failure_reason: Optional[str] = None
    wallet_txn_reference: Optional[str] = None
    merchant_ledger_reference: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


# --- Trip API Models ---
class TripUpdate(SQLModel):
    status: Optional[str] = None


class TripCreate(TripBase):
    user_id: Optional[uuid.UUID] = None
    driver_id: Optional[int] = None


class MechanicTripCreate(SQLModel):
    vehicle_type: str  # required, matches existing TripCreate contract
    user_id: Optional[uuid.UUID] = None
    mechanic_id: Optional[int] = None
    hiring_type: Optional[str] = None  # accepted for back-compat; ignored
    start_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    reason: Optional[str] = None
    fare: Optional[float] = None
    fare_breakdown: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class TowTripCreate(SQLModel):
    vehicle_type: str  # required, matches existing TripCreate contract
    service_type: str = "tow"  # "tow" | "transport"; user picks the service first
    tow_vehicle_type: Optional[str] = None  # requested tow-truck class
    transport_vehicle_type: Optional[str] = None  # requested transport class
    user_id: Optional[uuid.UUID] = None
    tow_truck_driver_id: Optional[int] = None
    hiring_type: Optional[str] = None  # accepted for back-compat; ignored
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
    reason: Optional[str] = None
    fare: Optional[float] = None
    fare_breakdown: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class BookingAddressUpdate(SQLModel):
    start_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_location: Optional[str] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None


class TripDaySkipRequest(SQLModel):
    trip_date: date
    reason: Optional[str] = None


class LocationUpdate(SQLModel):
    latitude: float
    longitude: float
    heading: Optional[float] = 0.0
    speed: Optional[float] = 0.0
    trip_id: Optional[str] = None  # TowTrip/MechanicTrip reference_id


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
    vehicle_type: Optional[str] = None
    # Tow Truck Specific Fields
    vehicle_number: Optional[str] = None
    service_type: Optional[str] = None  # "tow" | "transport" (tow providers)
    tow_vehicle_type: Optional[str] = None  # flatbed/wheel_lift/hook_chain/integrated
    transport_vehicle_type: Optional[str] = None  # mini_truck/tempo/pickup/...
    specialization: Optional[str] = None
    # Service Center Specific Fields
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # Center Member Specific Fields
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    center_code: Optional[str] = None  # 6-char code of the center they join
    expert_in: Optional[List[str]] = None  # subset of the center's service names


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


# ============= NEW TRIP MANAGEMENT MODELS =============


# --- OTP REGISTRY ---
class OTPRegistry(SQLModel, table=True):
    """
    Single OTP per shift (Uber-style):
    - User receives the OTP (push/SMS).
    - User reads it out to the driver.
    - Driver enters it in the driver app, which calls /verify-otp.

    Uniqueness: one row per TripAttendance (F4). Earlier the unique key was
    (trip_id, trip_date) which broke for cross-midnight shifts — a 22:00 Mon
    → 06:00 Tue shift either had no clear trip_date or collided with a
    separate Tue shift on multi-shift days. Keying off attendance_id removes
    the ambiguity entirely. `trip_date` remains for backwards-compatible
    queries but is no longer the uniqueness anchor.
    """

    __table_args__ = (UniqueConstraint("attendance_id", name="uq_otp_attendance"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True)
    # F4: nullable on the model so a backfill migration can populate rows
    # gradually, but new rows MUST set it. otp_service enforces non-null.
    attendance_id: Optional[int] = Field(
        default=None, foreign_key="tripattendance.id", index=True
    )
    trip_date: date = Field(index=True)
    otp_hash: str  # SHA-256 hash of the plain OTP (DB fallback when Redis is down)
    verified_at: Optional[datetime] = None  # Set when driver successfully verifies
    verified_by_driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")
    otp_expiry_at: datetime
    valid_from: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now_ist_naive)
    verification_attempts: int = 0
    max_attempts: int = 3


# NOTE: The legacy trip-only ``PaymentTransaction`` table was removed. Trip
# payments (driver acceptance fee, user upfront, daily bills, settlement,
# cancellation balance) now live on the centralized ``Payment`` table below,
# discriminated by ``purpose`` + ``payer_type``. See app.modules.trips.
# payment_orchestrator for the post-payment side effects.


# --- CENTRALIZED PAYMENTS (polymorphic across services) ---
class Payment(SQLModel, table=True):
    """One payment per booking-charge across any service type.

    Polymorphic link: (service_type, service_reference_id) point at the booking
    (tow / mechanic / service_center / trip). ``channel`` distinguishes
    platform-held gateway money, prepaid wallet, and direct cash/UPI collected
    by the provider.

    Trip flows make MANY charges per booking, so ``purpose`` discriminates them
    (driver_acceptance / user_upfront / daily_bill / settlement /
    cancellation_balance / schedule_diff) and ``payer_type`` records whether a
    user or a driver paid (the driver acceptance fee is paid by the driver).
    For non-trip services ``purpose`` is None and ``payer_type`` is "user".
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    reference_id: Optional[str] = Field(default=None, unique=True, index=True)

    service_type: str = Field(index=True)  # "tow"|"mechanic"|"service_center"|"trip"
    service_reference_id: str = Field(index=True)  # booking's reference_id
    service_id: int  # booking's internal PK (no hard FK — polymorphic)

    user_id: uuid.UUID = Field(foreign_key="user.id")  # payer's user account
    payee_type: str = "platform"  # "platform" | "driver"
    payee_driver_id: Optional[int] = None  # provider PK when direct-to-driver

    # Who actually pays. For trip driver-acceptance fees this is "driver" and
    # payer_driver_id is the Driver PK (user_id still holds the driver's own
    # User id so wallet refund routing keeps working). Defaults keep every
    # existing tow/mechanic/service row unchanged.
    payer_type: str = "user"  # "user" | "driver"
    payer_driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")

    # Trip charge discriminator (None for tow/mechanic/service).
    purpose: Optional[str] = Field(default=None, index=True)

    amount: float
    currency: str = "INR"
    channel: str  # "platform" | "wallet" | "cash" | "upi_direct"
    status: str = "created"  # created|pending|succeeded|failed|refunded|partially_refunded|cancelled
    refunded_amount: float = 0.0  # cumulative refunded (enables partial refunds)

    gateway_provider: str = "mock"
    gateway_intent_id: Optional[str] = Field(default=None, index=True)
    gateway_transaction_id: Optional[str] = None
    gateway_signature: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, index=True)

    extra: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)
    completed_at: Optional[datetime] = None


class PaymentIntentCreate(SQLModel):
    service_type: str
    service_reference_id: str
    # platform | upi | wallet | cash | upi_direct.  ``platform``/``upi`` both
    # settle to the platform merchant account via the gateway (card vs UPI rail).
    channel: str = "platform"
    amount: Optional[float] = None  # derived from the booking fare when omitted
    card_reference_id: Optional[str] = None


class PaymentPublic(SQLModel):
    """Safe payment view — exposes the reference id, never internal/gateway ids."""

    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    service_type: str
    service_reference_id: str
    purpose: Optional[str] = None
    amount: float
    currency: str
    channel: str
    status: str
    refunded_amount: float = 0.0
    created_at: datetime
    completed_at: Optional[datetime] = None


# --- PRICING COMPONENTS (for detailed billing) ---
class PricingComponentBreakdown(SQLModel, table=True):
    """Itemized breakdown of charges for transparency"""

    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    bill_id: Optional[int] = Field(default=None, foreign_key="tripbill.id")

    component_name: str  # "Base Fare", "Vehicle Allowance", "Tax", "Discount", etc.
    amount: float
    percentage: Optional[float] = None
    description: Optional[str] = None
    trip_date: Optional[date] = None
    created_at: datetime = Field(default_factory=_now_ist_naive)


# --- TRIP BILL ---
class TripBill(SQLModel, table=True):
    """Daily bill and final settlement"""

    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    driver_id: int = Field(foreign_key="driver.id")

    bill_type: str  # "daily_bill", "final_settlement"
    bill_date: date

    # Amounts
    total_amount: float
    amount_paid: float = 0.0
    amount_due: float
    discount_amount: Optional[float] = 0.0
    discount_percentage: Optional[float] = None  # e.g., 5% for full payment

    # State
    is_generated: bool = False
    is_paid: bool = False
    paid_at: Optional[datetime] = None
    paid_by: Optional[str] = None  # "user_online" | "driver_offline"
    paid_by_driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")
    payment_note: Optional[str] = None
    due_date: Optional[datetime] = None

    # Components stored as JSON: list of {"name": str, "amount": float, "percentage": float}
    components: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )

    notes: Optional[str] = None
    generated_at: datetime = Field(default_factory=_now_ist_naive)


# --- TRIP ATTENDANCE ---
class TripAttendance(SQLModel, table=True):
    """Tracks daily attendance for trip shifts"""

    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    trip_date: date

    # Attendance status
    status: str  # "present", "absent", "skipped_by_user", "skipped_by_driver"
    marked_by: str  # "user", "driver", "system"

    # OTP verification status
    user_otp_verified: bool = False
    driver_otp_verified: bool = False

    # Timing
    scheduled_start: datetime
    actual_start: Optional[datetime] = None
    scheduled_end: datetime
    actual_end: Optional[datetime] = None

    # Notes
    skip_reason: Optional[str] = None
    notes: Optional[str] = None

    created_at: datetime = Field(default_factory=_now_ist_naive)
    updated_at: datetime = Field(default_factory=_now_ist_naive)


# --- TRIP SETTLEMENT ---
class TripSettlement(SQLModel, table=True):
    """Final settlement record for multi-day trips"""

    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    driver_id: int = Field(foreign_key="driver.id")

    # Settlement details
    settlement_status: str  # "generated", "pending_user_approval", "approved", "paid"
    total_trips: int
    completed_trips: int
    absent_trips: int
    skipped_trips: int

    # Financial details
    total_earned: float
    total_paid_upfront: float
    remaining_due: float
    refund_amount: float = 0.0

    # Payment status
    user_payment_status: str = "pending"  # pending, paid, refunded
    driver_payment_status: str = "pending"  # pending, paid

    # Optional user note captured at settlement /pay time.
    payment_note: Optional[str] = None
    # Outstation only: any additional amount the user voluntarily paid
    # on top of remaining_due (e.g. toll, parking, food reimbursements).
    extra_amount_paid: float = 0.0
    # Itemised breakdown of `extra_amount_paid` so we have an audit trail of
    # what each reimbursement was for (F7). Keys are restricted to
    # {"toll", "parking", "food", "other"} and the values must sum to
    # extra_amount_paid — both invariants are validated at /settlement/pay time.
    extra_amount_breakdown: Optional[Dict[str, float]] = Field(
        default=None, sa_column=Column(JSON)
    )

    settlement_date: date
    due_date: Optional[date] = None
    paid_at: Optional[datetime] = None
    generated_at: datetime = Field(default_factory=_now_ist_naive)
    # F8 dunning bookkeeping. `dunning_stage` advances 0 → 1 (1d past due) →
    # 2 (3d) → 3 (7d) → 4 (14d) → 5 (28d) → 6 (collections handoff at 30d).
    # `collections_sent_at` is set when stage 6 fires so the worker doesn't
    # re-send the handoff event each tick.
    dunning_stage: int = 0
    last_reminder_at: Optional[datetime] = None
    collections_sent_at: Optional[datetime] = None


# ============= API REQUEST/RESPONSE MODELS =============


class FareEstimateRequest(SQLModel):
    """
    Booking inputs needed to compute the fare BEFORE the trip is created.
    Mirrors the bookable subset of TripCreate. The same calculator is used
    server-side at /trips/book-request, so the estimate is authoritative.

    Geographic resolution (Outstation):
      - State for permit fee is derived from `end_location` text (substring match
        against the known Indian state/UT list).
      - Distance is taken from `distance_km` if provided; otherwise computed via
        Haversine from start/end coords; otherwise treated as 0.
    """

    hiring_type: str  # "Daily" | "Monthly" | "Outstation"
    vehicle_type: str
    shift_details: Optional[str] = None  # e.g. "8 Hours (15:00)"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    months: Optional[int] = None
    selected_days: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
    booking_time: Optional[datetime] = (
        None  # used for night-surcharge check; defaults to now
    )


class UserPublicForDriver(SQLModel):
    """Trip-rider info exposed to the assigned driver.

    No UUID leak, and deliberately NO phone number or address — driver-app
    responses must never carry user contact/PII (privacy policy).
    """

    full_name: Optional[str] = None
    avatar_url: Optional[str] = None


class TripReadDriver(SQLModel):
    """Trip rows exposed to the driver app. Excludes user UUID and internal flags."""

    id: str = Field(validation_alias=AliasChoices("reference_id", "id"))
    hiring_type: str
    vehicle_type: str
    shift_details: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    months: Optional[int] = None
    selected_days: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    distance_km: Optional[float] = None
    reason: Optional[str] = None
    status: str
    payment_method: Optional[str] = None
    fare: Optional[float] = None
    fare_breakdown: Optional[Dict[str, Any]] = None
    booking_time: datetime
    scheduled_start_time: Optional[datetime] = None
    scheduled_end_time: Optional[datetime] = None
    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None
    trip_duration_hours: Optional[int] = None
    user: Optional[UserPublicForDriver] = None
    # Driver's remaining shift-skips on this trip booking this month (limit 3).
    driver_skips_remaining: Optional[int] = None


class TripBillRead(SQLModel):
    """Bill row exposed via the API. Hides user_id/driver_id internals."""

    id: int
    trip_id: str
    bill_type: str
    bill_date: date
    total_amount: float
    amount_paid: float
    amount_due: float
    discount_amount: Optional[float] = 0.0
    discount_percentage: Optional[float] = None
    is_generated: bool
    is_paid: bool
    paid_at: Optional[datetime] = None
    paid_by: Optional[str] = None
    payment_note: Optional[str] = None
    due_date: Optional[datetime] = None
    components: List[Dict[str, Any]] = []
    notes: Optional[str] = None
    generated_at: datetime


class BillResponse(SQLModel):
    """Response model for bill details"""

    id: int
    bill_type: str
    bill_date: date
    total_amount: float
    amount_paid: float
    amount_due: float
    discount_percentage: Optional[float] = None
    components: List[Dict[str, Any]]
    is_paid: bool


class SettlementResponse(SQLModel):
    """Response model for final settlement"""

    id: int
    total_trips: int
    completed_trips: int
    total_earned: float
    total_paid_upfront: float
    remaining_due: float
    settlement_date: date
    user_payment_status: str
    driver_payment_status: str
    payment_note: Optional[str] = None
    extra_amount_paid: float = 0.0
    extra_amount_breakdown: Optional[Dict[str, float]] = None
