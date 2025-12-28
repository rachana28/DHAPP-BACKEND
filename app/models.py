from typing import Optional
from datetime import datetime
from sqlmodel import Field, SQLModel
from pydantic import EmailStr

# --- Base Models (Shared fields) ---
class DriverBase(SQLModel):
    name: str
    phone_number: str
    license_number: str
    vehicle_type: Optional[str] = None
    status: str = "available"  # e.g., available, on_trip

class OrganisationBase(SQLModel):
    org_name: str = Field(index=True)  # Indexed for faster search
    contact_person: str
    contact_email: Optional[str] = None
    address: str
    is_active: bool = True

# --- Table Models (The actual database tables) ---
class Driver(DriverBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rating: float = Field(default=0.0)

class Organisation(OrganisationBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rating: float = Field(default=0.0)

# --- Update Models (For when we want to update only specific fields) ---
class DriverUpdate(SQLModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    status: Optional[str] = None

class OrganisationUpdate(SQLModel):
    org_name: Optional[str] = None
    contact_person: Optional[str] = None
    address: Optional[str] = None

# --- Review Models ---
class DriverReviewBase(SQLModel):
    user_id: int  # Assuming user_id is an integer
    rating: int = Field(ge=1, le=5)  # Rating between 1 and 5
    comment: Optional[str] = None

class DriverReview(DriverReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_id: int = Field(foreign_key="driver.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class OrganisationReviewBase(SQLModel):
    user_id: int
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None

class OrganisationReview(OrganisationReviewBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    organisation_id: int = Field(foreign_key="organisation.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

# --- Base Models ---
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True)
    full_name: Optional[str] = None
    provider: str = "local"
    avatar_url: Optional[str] = None

# --- Table Model (Database) ---
class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str

# --- API Request Models ---
class UserCreate(SQLModel):
    email: EmailStr
    password: str
    full_name: str

class UserLogin(SQLModel):
    email: EmailStr
    password: str

class Token(SQLModel):
    access_token: str
    token_type: str
