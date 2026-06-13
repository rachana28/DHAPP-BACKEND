"""Center-member self-service API (driver app).

A center-member is a worker under a service center. They are READ-ONLY on
bookings (the center drives all status changes) and have NO wallet/help. This
router exposes only: own profile (restricted edit), availability toggle,
profile photo, their assignments (paginated + redis-cached active), a sanitized
per-booking summary, and their aggregate reviews.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlmodel import Session, select, desc
from typing import List

from app.core.database import get_session
from app.core.models import (
    CenterMember,
    CenterMemberPublic,
    CenterMemberProfileUpdate,
    MemberAssignmentSummary,
    AvailabilityUpdate,
    ServiceCenter,
    ServiceRequest,
)
from app.core.security import get_current_active_center_member
from app.core import cache
from app.utils.booking_states import SERVICE_ACTIVE_STATES
from app.utils.storage import upload_profile_picture_to_r2
from app.modules.service.booking_summary import build_member_assignment_summary
from app.modules.service import assignment as member_assignment

router = APIRouter(prefix="/center-members", tags=["Center Members"])


def _member_me_key(member: CenterMember) -> str:
    return cache.me_key("center_member", member.id)


def _member_public(session: Session, member: CenterMember) -> CenterMemberPublic:
    center = session.get(ServiceCenter, member.service_center_id)
    return CenterMemberPublic(
        id=member.reference_id,
        name=member.name,
        phone_number=member.phone_number,
        profile_picture_url=member.profile_picture_url,
        date_of_birth=member.date_of_birth,
        gender=member.gender,
        expert_in=member.expert_in,
        status=member.status,
        is_online=member.is_online,
        rating=member.rating,
        total_reviews=member.total_reviews,
        center_name=center.name if center else None,
    )


# --- PROFILE ---


@router.get("/me", response_model=CenterMemberPublic)
def read_member_profile(
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
):
    """The member's own profile (driver app). Redis-cached; invalidated on
    profile/availability/photo writes."""
    key = _member_me_key(member)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached
    data = _member_public(session, member).model_dump(mode="json")
    cache.cache_set_json(key, data, cache.ME_CACHE_TTL)
    return data


@router.patch("/me", response_model=CenterMemberPublic)
def update_member_profile(
    body: CenterMemberProfileUpdate,
    *,
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
):
    """Restricted self-edit — ONLY name, date_of_birth and expert_in. (Gender /
    phone are fixed; profile photo has its own endpoint.) expert_in must stay a
    subset of the center's services."""
    if body.name is not None:
        member.name = body.name
    if body.date_of_birth is not None:
        member.date_of_birth = body.date_of_birth
    if body.expert_in is not None:
        center = session.get(ServiceCenter, member.service_center_id)
        valid = {s.service_name for s in center.services} if center else set()
        invalid = [e for e in body.expert_in if e not in valid]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "expert_in must be services offered by your center",
                    "invalid": invalid,
                    "valid_options": sorted(valid),
                },
            )
        member.expert_in = body.expert_in

    session.add(member)
    session.commit()
    session.refresh(member)
    cache.cache_delete(_member_me_key(member))
    return _member_public(session, member)


@router.put("/me/profile-picture", response_model=CenterMemberPublic)
async def update_member_profile_picture(
    *,
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
    file: UploadFile = File(...),
):
    """Upload/replace the member's profile photo (Cloudflare R2)."""
    public_url = await upload_profile_picture_to_r2(
        file, "center_member", str(member.id)
    )
    member.profile_picture_url = public_url
    session.add(member)
    session.commit()
    session.refresh(member)
    cache.cache_delete(_member_me_key(member))
    return _member_public(session, member)


@router.patch("/me/availability", response_model=CenterMemberPublic)
def set_member_availability(
    body: AvailabilityUpdate,
    *,
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
):
    """Online/offline toggle. Offline members are excluded from assignment
    (manual and auto)."""
    member.is_online = body.is_online
    session.add(member)
    session.commit()
    session.refresh(member)
    cache.cache_delete(_member_me_key(member))
    member_assignment.invalidate_member_active_cache(member.id)
    return _member_public(session, member)


# --- ASSIGNMENTS ---


@router.get("/me/my-assignments", response_model=List[MemberAssignmentSummary])
def get_my_assignments(
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
    status: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """Paginated history of bookings assigned to this member (sanitized — no
    price / payment / customer)."""
    query = select(ServiceRequest).where(
        ServiceRequest.assigned_member_id == member.id
    )
    if status:
        query = query.where(ServiceRequest.status == status)
    query = (
        query.order_by(desc(ServiceRequest.booking_time)).offset(offset).limit(limit)
    )
    rows = session.exec(query).all()
    return [
        MemberAssignmentSummary(**build_member_assignment_summary(session, b))
        for b in rows
    ]


@router.get("/me/active-assignments")
def get_my_active_assignments(
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
):
    """Driver-app polling target: the member's currently-engaged assignments.
    Short-TTL redis-cached; sanitized."""
    if member.status != "approved":
        return []
    key = cache.active_key("center_member", member.id)
    cached = cache.cache_get_json(key)
    if cached is not None:
        return cached

    rows = session.exec(
        select(ServiceRequest)
        .where(
            ServiceRequest.assigned_member_id == member.id,
            ServiceRequest.status.in_(SERVICE_ACTIVE_STATES),
        )
        .order_by(desc(ServiceRequest.booking_time))
    ).all()
    result = [
        MemberAssignmentSummary(
            **build_member_assignment_summary(session, b)
        ).model_dump(mode="json")
        for b in rows
    ]
    cache.cache_set_json(key, result, cache.ACTIVE_CACHE_TTL)
    return result


@router.get(
    "/me/assignments/{booking_ref}/summary",
    response_model=MemberAssignmentSummary,
)
def get_assignment_summary(
    booking_ref: str,
    session: Session = Depends(get_session),
    member: CenterMember = Depends(get_current_active_center_member),
):
    """Sanitized summary of a single assigned booking (no price/payment/customer
    info). 404 if it isn't assigned to this member."""
    booking = session.exec(
        select(ServiceRequest).where(ServiceRequest.reference_id == booking_ref)
    ).first()
    if not booking or booking.assigned_member_id != member.id:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return MemberAssignmentSummary(**build_member_assignment_summary(session, booking))
