from typing import Optional, List
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
from pydantic import EmailStr


# --- Trip Models ---
class TripBase(SQLModel):
    user_id: int = Field(foreign_key="user.id")
    driver_id: int = Field(foreign_key="driver.id")
    start_location: str
    end_location: str
    fare: Optional[float] = None
    status: str = (
        "pending"  # e.g., pending, accepted, in_progress, completed, cancelled
    )
    booking_time: datetime = Field(default_factory=datetime.utcnow)


class Trip(TripBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver: "Driver" = Relationship(back_populates="trips")
    user: "User" = Relationship(back_populates="trips")


# --- Base Models (Shared fields) ---
class DriverBase(SQLModel):
    name: str
    phone_number: str  # Sensitive
    license_number: str  # Sensitive
    address: Optional[str] = None  # Sensitive
    emergency_phone: Optional[str] = None  # Sensitive

    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None
    fare_per_km: Optional[float] = None
    driver_allowance: Optional[float] = None
    spoken_languages: Optional[str] = None  # Comma-separated

    status: str = "available"  # e.g., available, on_trip


class OrganisationBase(SQLModel):
    org_name: str = Field(index=True)
    contact_number: str
    contact_email: Optional[str] = None
    address: str
    is_active: bool = True


# --- Table Models (The actual database tables) ---
class Driver(DriverBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    rating: float = Field(default=0.0)
    user: "User" = Relationship(back_populates="driver_profile")
    trips: List[Trip] = Relationship(back_populates="driver")
    reviews: List["DriverReview"] = Relationship(back_populates="driver")


class Organisation(OrganisationBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    rating: float = Field(default=0.0)


# --- API Response Models (Public/Private Views) ---
class DriverPublic(SQLModel):
    id: int
    name: str
    rating: float
    years_of_experience: Optional[int]
    vehicle_type: Optional[str]
    spoken_languages: Optional[str]
    status: str
    total_trips: int = 0


class DriverPrivate(DriverBase):
    id: int
    user_id: int
    rating: float


# --- Update Models (For when we want to update only specific fields) ---
class DriverUpdate(SQLModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone_number: Optional[str] = None
    emergency_phone: Optional[str] = None
    years_of_experience: Optional[int] = None
    vehicle_type: Optional[str] = None
    fare_per_km: Optional[float] = None
    driver_allowance: Optional[float] = None
    spoken_languages: Optional[str] = None
    status: Optional[str] = None


class OrganisationUpdate(SQLModel):
    org_name: Optional[str] = None
    contact_number: Optional[str] = None
    address: Optional[str] = None


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


class OrganisationReviewBase(SQLModel):
    user_id: int
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


class OrganisationReview(OrganisationReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    organisation_id: int = Field(foreign_key="organisation.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


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


class UserCreate(SQLModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: str = "user"
    # Driver fields
    license_number: Optional[str] = None
    vehicle_type: Optional[str] = None
    phone_number: Optional[str] = None
    # Organisation fields
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
    pass


class TripPublic(SQLModel):
    id: int
    start_location: str
    end_location: str
    status: str
    booking_time: datetime
    driver: DriverPublic
