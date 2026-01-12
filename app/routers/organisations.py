import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, select, func, desc
from typing import List
import redis

# Import our setup
from app.database import get_session, get_redis
from app.models import (
    Organisation,
    OrganisationBase,
    OrganisationUpdate,
    OrganisationReview,
    OrganisationReviewBase,
)

# Create a router specifically for organisations
router = APIRouter(prefix="/organisations", tags=["Organisations"])


# 1. Create an Organisation
@router.post("/", response_model=Organisation)
def create_organisation(
    org: OrganisationBase,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    db_org = Organisation.model_validate(org)
    session.add(db_org)
    session.commit()
    session.refresh(db_org)

    if redis_client:
        redis_client.delete("organisations")

    return db_org


# 2. Read All Organisations
@router.get("/", response_model=List[Organisation])
def read_organisations(
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    if redis_client:
        cached_orgs = redis_client.get("organisations")
        if cached_orgs:
            return json.loads(cached_orgs)

    organisations = session.exec(select(Organisation)).all()

    if redis_client:
        redis_client.set(
            "organisations", json.dumps(jsonable_encoder(organisations)), ex=3600
        )

    return organisations


# 3. Read One Organisation by ID
@router.get("/{org_id}", response_model=Organisation)
def read_organisation(
    org_id: int,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    if redis_client:
        cached_org = redis_client.get(f"organisation_{org_id}")
        if cached_org:
            return json.loads(cached_org)

    org = session.get(Organisation, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")

    if redis_client:
        redis_client.set(f"organisation_{org_id}", org.model_dump_json(), ex=3600)

    return org


# 4. Update an Organisation
@router.patch("/{org_id}", response_model=Organisation)
def update_organisation(
    org_id: int,
    org_update: OrganisationUpdate,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    db_org = session.get(Organisation, org_id)
    if not db_org:
        raise HTTPException(status_code=404, detail="Organisation not found")

    org_data = org_update.model_dump(exclude_unset=True)
    for key, value in org_data.items():
        setattr(db_org, key, value)

    session.add(db_org)
    session.commit()
    session.refresh(db_org)

    if redis_client:
        redis_client.delete("organisations")
        redis_client.delete(f"organisation_{org_id}")

    return db_org


# 5. Delete an Organisation
@router.delete("/{org_id}")
def delete_organisation(
    org_id: int,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    org = session.get(Organisation, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
    session.delete(org)
    session.commit()

    if redis_client:
        redis_client.delete("organisations")
        redis_client.delete(f"organisation_{org_id}")

    return {"ok": True}


# 6. Get all reviews for an organisation
@router.get("/{org_id}/reviews", response_model=List[OrganisationReview])
def get_organisation_reviews(org_id: int, session: Session = Depends(get_session)):
    # Check if organisation exists
    org = session.get(Organisation, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")

    reviews = session.exec(
        select(OrganisationReview)
        .where(OrganisationReview.organisation_id == org_id)
        .order_by(desc(OrganisationReview.created_at))
    ).all()
    return reviews


# 7. Add a review for an organisation
@router.post("/{org_id}/reviews", response_model=OrganisationReview)
def add_organisation_review(
    org_id: int,
    review: OrganisationReviewBase,
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    # Check if organisation exists
    org = session.get(Organisation, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")

    db_review = OrganisationReview.model_validate(
        review, update={"organisation_id": org_id}
    )
    session.add(db_review)
    session.commit()
    session.refresh(db_review)

    # Update organisation's average rating
    avg_rating = session.exec(
        select(func.avg(OrganisationReview.rating)).where(
            OrganisationReview.organisation_id == org_id
        )
    ).first()
    org.rating = round(avg_rating, 2) if avg_rating is not None else 0.0
    session.add(org)
    session.commit()

    if redis_client:
        redis_client.delete("organisations")
        redis_client.delete(f"organisation_{org_id}")

    return db_review
