from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select
import uuid

from app.core.database import get_session
from app.core.models import User, Driver, TowTruckDriver

SECRET_KEY = "supersecretkey_change_this_in_production"
REFRESH_SECRET_KEY = "refresh_supersecretkey_change_this_too"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh", "jti": str(uuid.uuid4())})
    encoded_jwt = jwt.encode(to_encode, REFRESH_SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_refresh_token(token: str, session: Session):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, REFRESH_SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        token_type: str = payload.get("type")

        if email is None or role is None or token_type != "refresh":
            raise credentials_exception

        # Optional: Check if user still exists/is active here
        user = session.exec(
            select(User).where(User.email == email).where(User.role == role)
        ).first()

        if user is None:
            raise credentials_exception

        return user
    except JWTError:
        raise credentials_exception


def get_current_user(
    token: str = Depends(oauth2_scheme), session: Session = Depends(get_session)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        if email is None or role is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = session.exec(
        select(User).where(User.email == email).where(User.role == role)
    ).first()

    if user is None:
        raise credentials_exception
    return user


def get_current_active_driver(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Driver:
    if current_user.role != "driver":
        raise HTTPException(status_code=403, detail="Not a driver")

    driver_profile = session.exec(
        select(Driver).where(Driver.user_id == current_user.id)
    ).first()
    if not driver_profile:
        raise HTTPException(status_code=404, detail="Driver profile not found")

    return driver_profile


def get_current_active_tow_truck_driver(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> TowTruckDriver:
    if current_user.role != "tow_truck_driver":
        raise HTTPException(status_code=403, detail="Not a tow truck driver")

    driver_profile = session.exec(
        select(TowTruckDriver).where(TowTruckDriver.user_id == current_user.id)
    ).first()
    if not driver_profile:
        raise HTTPException(
            status_code=404, detail="Tow truck driver profile not found"
        )

    return driver_profile


def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if (
        current_user.role == "user"
        or current_user.role == "driver"
        or current_user.role == "tow_truck_driver"
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Not a valid user")
