import random
import redis
import os
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, delete
from datetime import datetime
from app.core.database import get_session, get_redis
from app.core.models import (
    User,
    Token,
    Driver,
    TowTruckDriver,
    VerifyOTPRequest,
    SendOTPRequest,
    UserDevice,
)
from app.core.security import (
    get_password_hash,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    get_current_user,
)
import requests
import re
from dns import resolver
from pydantic import BaseModel, EmailStr

router = APIRouter(prefix="/auth", tags=["Authentication"])

FAST2SMS_API_KEY = os.getenv("FAST2SMS_API_KEY")
DAILY_SMS_LIMIT = 50
BYPASS_NUMBERS = ["9999999999", "9876543210", "+919999999999", "+919876543210"]


class EmailVerificationRequest(BaseModel):
    email: EmailStr


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class DeviceTokenRequest(BaseModel):
    token: str
    platform: str = "unknown"


class PasswordChangeRequest(BaseModel):
    new_password: str


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


@router.post("/send-otp")
def send_otp(request: SendOTPRequest, redis_client: redis.Redis = Depends(get_redis)):
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis connection failed")

    phone = request.phone_number

    # Rate Limiting
    daily_key = f"daily_otp_limit:{phone}"
    current_count = redis_client.get(daily_key)

    if current_count and int(current_count) >= DAILY_SMS_LIMIT:
        raise HTTPException(
            status_code=429, detail="Daily OTP limit reached. Try again tomorrow."
        )

    # Static OTP for Apple/Google App Review bypass
    if phone in BYPASS_NUMBERS:
        otp = "1234"
    else:
        otp = str(random.randint(1000, 9999))

    # Store OTP (Valid for 5 mins)
    redis_client.setex(f"otp:{phone}", 300, otp)

    # Send via Fast2SMS
    if phone not in BYPASS_NUMBERS:
        url = "https://www.fast2sms.com/dev/bulkV2"
        payload = f"variables_values={otp}&route=otp&numbers={phone[-10:]}"
        headers = {
            "authorization": FAST2SMS_API_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            response = requests.post(url, data=payload, headers=headers)
            res_data = response.json()
            if not res_data.get("return"):
                raise HTTPException(
                    status_code=500, detail="Failed to send SMS via provider"
                )
        except Exception:
            raise HTTPException(status_code=500, detail="SMS provider error")

    # Increment counter
    redis_client.incr(daily_key)
    if not current_count:
        redis_client.expire(daily_key, 86400)

    return {"message": f"OTP sent successfully for role: {request.role}"}


@router.post("/verify-otp", response_model=Token)
def verify_otp(
    request: VerifyOTPRequest,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    phone = request.phone_number
    role = request.role

    # 1. Verify OTP
    stored_otp = redis_client.get(f"otp:{phone}")
    if not stored_otp or stored_otp != request.otp:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # Clear OTP
    redis_client.delete(f"otp:{phone}")

    # 2. Check Database
    user = session.exec(
        select(User).where(User.phone_number == phone, User.role == role)
    ).first()

    # 3. Handle Registration / Signup
    if not user:
        if not request.full_name:
            raise HTTPException(
                status_code=400, detail="Full name required for new registration"
            )

        # Validate Specific Roles
        if role == "driver":
            if not request.license_number:
                raise HTTPException(
                    status_code=400,
                    detail="License number required for driver registration",
                )
        elif role == "tow_truck_driver":
            if not request.vehicle_number:
                raise HTTPException(
                    status_code=400,
                    detail="Vehicle number required for tow truck registration",
                )

        # Create Base User
        user = User(
            phone_number=phone,
            email=request.email,  # Include email if provided
            full_name=request.full_name,
            role=role,
            provider="local",
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        # Create Role Profile
        if role == "driver":
            db_driver = Driver(
                name=request.full_name,
                phone_number=phone,
                license_number=request.license_number,
                vehicle_type=request.vehicle_type,
                user_id=user.id,
            )
            session.add(db_driver)
            session.commit()

        elif role == "tow_truck_driver":
            db_tow_driver = TowTruckDriver(
                name=request.full_name,
                phone_number=phone,
                vehicle_number=request.vehicle_number,
                user_id=user.id,
            )
            session.add(db_tow_driver)
            session.commit()

    # 4. Generate Session Tokens
    access_token = create_access_token(
        data={"sub": user.phone_number, "role": user.role}
    )
    refresh_token = create_refresh_token(
        data={"sub": user.phone_number, "role": user.role}
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "name": user.full_name,
            "phone_number": user.phone_number,
            "role": user.role,
        },
    }


@router.post("/change-password")
def change_password(
    data: PasswordChangeRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(
        get_current_user
    ),  # Use get_current_user (NOT Admin) so they aren't blocked
):
    """
    Endpoint to set a new password.
    Can be used by any authenticated user, but specifically clears the force_password_change flag.
    """
    hashed_pwd = get_password_hash(data.new_password)

    current_user.hashed_password = hashed_pwd
    current_user.force_password_change = False  # Clear the flag

    session.add(current_user)
    session.commit()

    return {"message": "Password updated successfully. You may proceed to dashboard."}


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


@router.post("/logout")
def logout(
    data: DeviceTokenRequest,  # Mobile app must send the token it wants to remove
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Removes the specific device token on logout so notifications stop.
    """
    statement = delete(UserDevice).where(
        UserDevice.user_id == current_user.id, UserDevice.token == data.token
    )
    session.exec(statement)
    session.commit()

    return {"message": "Logged out and device token removed"}


@router.post("/device/register")
def register_device(
    data: DeviceTokenRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Registers or updates a device push token for the current user.
    """
    # Check if token already exists for this user
    statement = select(UserDevice).where(
        UserDevice.user_id == current_user.id, UserDevice.token == data.token
    )
    existing_device = session.exec(statement).first()

    if existing_device:
        existing_device.last_updated = datetime.utcnow()
        existing_device.platform = data.platform
        session.add(existing_device)
    else:
        new_device = UserDevice(
            user_id=current_user.id, token=data.token, platform=data.platform
        )
        session.add(new_device)

    session.commit()
    return {"message": "Device registered successfully"}
