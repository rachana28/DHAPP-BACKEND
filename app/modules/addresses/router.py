"""
User address book.

  GET    /addresses          list the user's active addresses
  POST   /addresses          add an address (string + lat/lng JSON, label)
  PATCH  /addresses/{ref}     update an address
  DELETE /addresses/{ref}     soft-delete an address

Each address stores the full string in ``address_line`` and coordinates in a
``location`` JSON object ``{"lat": .., "lng": ..}``. Exactly one address is the
default; setting a new default clears the previous one.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.models import (
    User,
    UserAddress,
    UserAddressCreate,
    UserAddressUpdate,
    UserAddressPublic,
)
from app.core.security import get_current_user, get_current_user_no_member
from app.utils.id_generator import generate_reference_id, get_by_reference, ADDRESS
from app.utils.time_utils import now_ist

# Center-members have no address book — blocked at the router level.
router = APIRouter(
    prefix="/addresses",
    tags=["Addresses"],
    dependencies=[Depends(get_current_user_no_member)],
)


def _active_addresses(session: Session, user_id):
    return session.exec(
        select(UserAddress).where(
            UserAddress.user_id == user_id,
            UserAddress.is_active == True,  # noqa: E712
        )
    ).all()


def _unset_other_defaults(session: Session, user_id, keep_id) -> None:
    for addr in _active_addresses(session, user_id):
        if addr.id != keep_id and addr.is_default:
            addr.is_default = False
            addr.updated_at = now_ist()
            session.add(addr)


def _owned_active(session: Session, reference_id: str, user_id) -> UserAddress:
    addr = get_by_reference(session, UserAddress, reference_id)
    if not addr or addr.user_id != user_id or not addr.is_active:
        raise HTTPException(404, "Address not found")
    return addr


@router.get("", response_model=List[UserAddressPublic])
def list_addresses(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    rows = session.exec(
        select(UserAddress)
        .where(
            UserAddress.user_id == current_user.id,
            UserAddress.is_active == True,  # noqa: E712
        )
        .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
    ).all()
    return rows


@router.post("", response_model=UserAddressPublic)
def create_address(
    data: UserAddressCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    location = {}
    if data.lat is not None and data.lng is not None:
        location = {"lat": data.lat, "lng": data.lng}

    # The very first address is always the default.
    is_first = len(_active_addresses(session, current_user.id)) == 0
    make_default = data.is_default or is_first

    addr = UserAddress(
        reference_id=generate_reference_id(session, ADDRESS),
        user_id=current_user.id,
        label=data.label,
        address_line=data.address_line,
        location=location,
        is_default=make_default,
    )
    session.add(addr)
    session.flush()  # assign addr.id before clearing other defaults

    if make_default:
        _unset_other_defaults(session, current_user.id, keep_id=addr.id)

    session.commit()
    session.refresh(addr)
    return addr


@router.patch("/{reference_id}", response_model=UserAddressPublic)
def update_address(
    reference_id: str,
    data: UserAddressUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    addr = _owned_active(session, reference_id, current_user.id)
    update_data = data.model_dump(exclude_unset=True)

    if "label" in update_data:
        addr.label = update_data["label"]
    if "address_line" in update_data:
        addr.address_line = update_data["address_line"]
    if "lat" in update_data or "lng" in update_data:
        loc = dict(addr.location or {})
        if "lat" in update_data:
            loc["lat"] = update_data["lat"]
        if "lng" in update_data:
            loc["lng"] = update_data["lng"]
        addr.location = loc

    if update_data.get("is_default") is True:
        addr.is_default = True
        _unset_other_defaults(session, current_user.id, keep_id=addr.id)
    elif update_data.get("is_default") is False:
        addr.is_default = False

    addr.updated_at = now_ist()
    session.add(addr)
    session.commit()
    session.refresh(addr)
    return addr


@router.delete("/{reference_id}")
def delete_address(
    reference_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    addr = _owned_active(session, reference_id, current_user.id)
    was_default = addr.is_default
    addr.is_active = False
    addr.is_default = False
    addr.updated_at = now_ist()
    session.add(addr)

    # Promote another address to default so the user always has one selected.
    if was_default:
        remaining = [
            a for a in _active_addresses(session, current_user.id) if a.id != addr.id
        ]
        if remaining:
            promote = max(remaining, key=lambda a: a.id)
            promote.is_default = True
            promote.updated_at = now_ist()
            session.add(promote)

    session.commit()
    return {"status": "deleted", "id": reference_id}
