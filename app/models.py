from typing import Optional, List
from datetime import datetime, date
from sqlmodel import Field, SQLModel, Relationship
from pydantic import EmailStr


# --- Base Models (Shared fields) ---
class DriverBase(SQLModel):
    name: str
    phone_number: str
    license_number: str
    address: Optional[str] = None
    emergency_phone: Optional[str] = None
    profile_picture_url: Optional[str] = None

    # Professional Details
    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None  # SEDAN, SUV, etc.
    fare_per_km: Optional[float] = None
    driver_allowance: Optional[float] = None
    spoken_languages: Optional[str] = None
    status: str = "available"  # available, on_trip, busy


# --- Trip Models ---
class TripBase(SQLModel):
    user_id: int = Field(foreign_key="user.id")
    driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")

    # Booking Details
    hiring_type: str
    vehicle_type: str
    shift_details: Optional[str] = None
    start_date: date
    end_date: date
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    reason: Optional[str] = None
    fare: Optional[float] = None
    status: str = "searching"
    booking_time: datetime = Field(default_factory=datetime.utcnow)


class Trip(TripBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver: Optional["Driver"] = Relationship(back_populates="trips")
    user: "User" = Relationship(back_populates="trips")
    offers: List["TripOffer"] = Relationship(back_populates="trip")


class TripOffer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id")
    driver_id: int = Field(foreign_key="driver.id")
    status: str = "pending"
    tier: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

    trip: Trip = Relationship(back_populates="offers")
    driver: "Driver" = Relationship(back_populates="offers")


# --- SAFETY LAYER: RESPONSE MODELS ---
class TripSafe(SQLModel):
    id: int
    hiring_type: str
    vehicle_type: str
    shift_details: Optional[str] = None
    start_date: date
    end_date: date
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    reason: Optional[str] = None
    status: str
    fare: Optional[float] = None
    booking_time: datetime


class TripOfferPublic(SQLModel):
    id: int  # Offer ID
    status: str
    tier: int
    created_at: datetime
    trip: TripSafe


# --- Table Models ---
class Driver(DriverBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    rating: float = Field(default=0.0)

    user: "User" = Relationship(back_populates="driver_profile")
    trips: List[Trip] = Relationship(back_populates="driver")
    reviews: List["DriverReview"] = Relationship(back_populates="driver")
    offers: List[TripOffer] = Relationship(back_populates="driver")


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


class DriverPrivate(DriverBase):
    id: int
    rating: float


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


# --- Review Models ---
class DriverReviewBase(SQLModel):
    user_id: int
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class DriverReview(DriverReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_id: int = Field(foreign_key="driver.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    driver: "Driver" = Relationship(back_populates="reviews")


# --- User Models ---
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True)
    full_name: Optional[str] = None
    provider: str = "local"
    avatar_url: Optional[str] = None
    role: str = "user"


class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str
    driver_profile: Optional[Driver] = Relationship(back_populates="user")
    trips: List[Trip] = Relationship(back_populates="user")


class UserPublic(SQLModel):
    id: int
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None


class UserPrivate(UserBase):
    id: int


class UserUpdate(SQLModel):
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None


class UserCreate(SQLModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: str = "user"
    # Optional fields for Driver/Org creation
    license_number: Optional[str] = None
    vehicle_type: Optional[str] = None
    phone_number: Optional[str] = None
    org_name: Optional[str] = None
    contact_number: Optional[str] = None
    address: Optional[str] = None


class UserLogin(SQLModel):
    email: EmailStr
    password: str
    role: str


class Token(SQLModel):
    access_token: str
    token_type: str
    user: dict


# --- Trip API Models ---
class TripUpdate(SQLModel):
    status: Optional[str] = None


class TripCreate(TripBase):
    # Overriding these as they are set by the system, not the user
    user_id: Optional[int] = None
    driver_id: Optional[int] = None
