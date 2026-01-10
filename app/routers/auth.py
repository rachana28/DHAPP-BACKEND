from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.database import get_session
from app.models import User, UserCreate, UserLogin, Token, Driver, Organisation
from app.security import get_password_hash, verify_password, create_access_token
import requests
import re
from dns import resolver
from pydantic import BaseModel, EmailStr

router = APIRouter(prefix="/auth", tags=["Authentication"])

class EmailVerificationRequest(BaseModel):
    email: EmailStr


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
def signup(user: UserCreate, session: Session = Depends(get_session)):
    # Validate based on role
    if user.role == "user":
        if not user.full_name:
            raise HTTPException(
                status_code=400, detail="Full name is required for user"
            )
    elif user.role == "driver":
        if not user.full_name or not user.license_number or not user.phone_number:
            raise HTTPException(
                status_code=400,
                detail="Full name, license number, and phone number are required for driver",
            )
    elif user.role == "organisation":
        if not user.org_name or not user.contact_number or not user.address:
            raise HTTPException(
                status_code=400,
                detail="Organisation name, contact number, and address are required for organisation",
            )
    else:
        raise HTTPException(status_code=400, detail="Invalid role")

    # Check for duplicates
    statement = select(User).where(User.email == user.email, User.role == user.role)
    existing_user = session.exec(statement).first()
    if existing_user:
        raise HTTPException(
            status_code=400, detail="Email already registered for this role"
        )

    # Hash the password
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

    # Create role-specific entity
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
    elif user.role == "organisation":
        db_org = Organisation(
            org_name=user.org_name,
            contact_number=user.contact_number,
            address=user.address,
            user_id=db_user.id,
        )
        session.add(db_org)
        session.commit()

    access_token = create_access_token(data={"sub": db_user.email})
    return {
        "access_token": access_token,
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
    return {
        "access_token": access_token,
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
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {"name": user.full_name, "picture": user.avatar_url, "role": user.role},
    }
