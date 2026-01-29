import redis
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.core.database import get_session, get_redis
from app.core.models import User, UserCreate, UserLogin, Token, Driver, TowTruckDriver
from app.core.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
)
import requests
import re
from dns import resolver
from pydantic import BaseModel, EmailStr

router = APIRouter(prefix="/auth", tags=["Authentication"])


class EmailVerificationRequest(BaseModel):
    email: EmailStr


class RefreshTokenRequest(BaseModel):
    refresh_token: str


@router.post("/verify-email")
def verify_email(request: EmailVerificationRequest):
    email = request.email
    # Simple regex for basic email format validation
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        raise HTTPException(status_code=422, detail="Invalid email format")

    domain = email.split("@")[1]

    try:
        # Check for MX records
        mx_records = resolver.resolve(domain, "MX")
        if not mx_records:
            raise HTTPException(status_code=422, detail="Domain does not accept mail")
    except resolver.NoAnswer:
        raise HTTPException(
            status_code=422, detail="No MX records found for the domain"
        )
    except resolver.NXDOMAIN:
        raise HTTPException(status_code=422, detail="Domain does not exist")
    except Exception as e:
        # Catch other potential DNS errors
        raise HTTPException(
            status_code=500, detail=f"An error occurred during DNS resolution: {e}"
        )

    return {"message": "Email is valid"}


@router.post("/signup", response_model=Token)
def signup(
    user: UserCreate,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    # Validate based on role
    if user.role == "user":
        if not user.full_name:
            raise HTTPException(status_code=400, detail="Full name is required")
    elif user.role == "driver":
        if not user.full_name or not user.license_number or not user.phone_number:
            raise HTTPException(status_code=400, detail="Missing driver fields")
    elif user.role == "tow_truck_driver":
        # Check required fields for Tow Truck Driver
        if not user.full_name or not user.vehicle_number or not user.phone_number:
            raise HTTPException(
                status_code=400,
                detail="Full name, vehicle number, and phone number are required for tow truck driver",
            )
    else:
        raise HTTPException(status_code=400, detail="Invalid role")

    # Check duplicates
    statement = select(User).where(User.email == user.email, User.role == user.role)
    if session.exec(statement).first():
        raise HTTPException(
            status_code=400, detail="Email already registered for this role"
        )

    # Create User
    hashed_pwd = get_password_hash(user.password)
    db_user = User(
        email=user.email,
        hashed_password=hashed_pwd,
        full_name=user.full_name,
        provider="local",
        role=user.role,
    )
    session.add(db_user)
    session.commit()
    session.refresh(db_user)

    # Create Role Profile
    if user.role == "driver":
        db_driver = Driver(
            name=user.full_name,
            phone_number=user.phone_number,
            license_number=user.license_number,
            vehicle_type=user.vehicle_type,
            user_id=db_user.id,
        )
        session.add(db_driver)
        session.commit()

    elif user.role == "tow_truck_driver":
        db_tow_driver = TowTruckDriver(
            name=user.full_name,
            phone_number=user.phone_number,
            vehicle_number=user.vehicle_number,
            user_id=db_user.id,
        )
        session.add(db_tow_driver)
        session.commit()

    access_token = create_access_token(
        data={"sub": db_user.email, "role": db_user.role}
    )
    refresh_token = create_refresh_token(
        data={"sub": db_user.email, "role": db_user.role}
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "name": db_user.full_name,
            "picture": db_user.avatar_url,
            "role": db_user.role,
        },
    }


@router.post("/login", response_model=Token)
def login(user_data: UserLogin, session: Session = Depends(get_session)):
    statement = select(User).where(
        User.email == user_data.email, User.role == user_data.role
    )
    user = session.exec(statement).first()

    if not user or not verify_password(user_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    access_token = create_access_token(data={"sub": user.email, "role": user.role})
    refresh_token = create_refresh_token(data={"sub": user.email, "role": user.role})
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {"name": user.full_name, "picture": user.avatar_url, "role": user.role},
    }


@router.post("/refresh", response_model=Token)
def refresh_token(
    request: RefreshTokenRequest, session: Session = Depends(get_session)
):
    user = verify_refresh_token(request.refresh_token, session)

    # Token Rotation: Issue new Access AND new Refresh token (safer)
    new_access_token = create_access_token(data={"sub": user.email, "role": user.role})
    new_refresh_token = create_refresh_token(
        data={"sub": user.email, "role": user.role}
    )

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "user": {"name": user.full_name, "picture": user.avatar_url, "role": user.role},
    }


@router.post("/google", response_model=Token)
def google_login(token: str, session: Session = Depends(get_session)):
    google_response = requests.get(
        f"https://oauth2.googleapis.com/tokeninfo?id_token={token}"
    )
    if google_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid Google Token")

    data = google_response.json()
    email = data["email"]
    name = data.get("name", "")
    picture = data.get("picture", "")

    statement = select(User).where(User.email == email, User.role == "user")
    user = session.exec(statement).first()

    if not user:
        user = User(
            email=email,
            hashed_password="google_oauth_user",
            full_name=name,
            provider="google",
            avatar_url=picture,
            role="user",  # Default to user for Google login
        )
        session.add(user)
        session.commit()

    access_token = create_access_token(data={"sub": user.email, "role": user.role})
    refresh_token = create_refresh_token(data={"sub": user.email, "role": user.role})
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {"name": user.full_name, "picture": user.avatar_url, "role": user.role},
    }
