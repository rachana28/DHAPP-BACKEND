from typing import Optional
from sqlmodel import Field, SQLModel

# --- Base Models (Shared fields) ---
class DriverBase(SQLModel):
   name: str
   phone_number: str
   license_number: str
   vehicle_type: Optional[str] = None
   status: str = "available"  # e.g., available, on_trip

class OrganisationBase(SQLModel):
   org_name: str = Field(index=True) # Indexed for faster search
   contact_person: str
   contact_email: Optional[str] = None
   address: str
   is_active: bool = True

# --- Table Models (The actual database tables) ---
class Driver(DriverBase, table=True):
   id: Optional[int] = Field(default=None, primary_key=True)

class Organisation(OrganisationBase, table=True):
   id: Optional[int] = Field(default=None, primary_key=True)

# --- Update Models (For when we want to update only specific fields) ---
class DriverUpdate(SQLModel):
   name: Optional[str] = None
   phone_number: Optional[str] = None
   status: Optional[str] = None

class OrganisationUpdate(SQLModel):
   org_name: Optional[str] = None
   contact_person: Optional[str] = None
   address: Optional[str] = None