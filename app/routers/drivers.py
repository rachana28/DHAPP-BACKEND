from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, func
from typing import List
from app.database import get_session
from app.models import Driver, DriverBase, DriverUpdate, DriverReview, DriverReviewBase

router = APIRouter(prefix="/drivers", tags=["Drivers"])

@router.post("/", response_model=Driver)
def create_driver(driver: DriverBase, session: Session = Depends(get_session)):
   db_driver = Driver.model_validate(driver)
   session.add(db_driver)
   session.commit()
   session.refresh(db_driver)
   return db_driver

@router.get("/", response_model=List[Driver])
def read_drivers(session: Session = Depends(get_session)):
   return session.exec(select(Driver)).all()

@router.get("/{driver_id}", response_model=Driver)
def read_driver(driver_id: int, session: Session = Depends(get_session)):
   driver = session.get(Driver, driver_id)
   if not driver:
       raise HTTPException(status_code=404, detail="Driver not found")
   return driver

@router.get("/{driver_id}/reviews/total")
def get_driver_reviews_total(driver_id: int, session: Session = Depends(get_session)):
   # Check if driver exists
   driver = session.get(Driver, driver_id)
   if not driver:
       raise HTTPException(status_code=404, detail="Driver not found")

   # Get total reviews and average rating
   result = session.exec(
       select(func.count(DriverReview.id), func.avg(DriverReview.rating))
       .where(DriverReview.driver_id == driver_id)
   ).first()

   total_reviews, avg_rating = result
   return {
       "driver_id": driver_id,
       "total_reviews": total_reviews or 0,
       "average_rating": round(avg_rating, 2) if avg_rating else 0.0
   }

@router.get("/{driver_id}/reviews", response_model=List[DriverReview])
def get_driver_reviews(driver_id: int, session: Session = Depends(get_session)):
   # Check if driver exists
   driver = session.get(Driver, driver_id)
   if not driver:
       raise HTTPException(status_code=404, detail="Driver not found")

   reviews = session.exec(select(DriverReview).where(DriverReview.driver_id == driver_id)).all()
   return reviews

@router.post("/{driver_id}/reviews", response_model=DriverReview)
def add_driver_review(driver_id: int, review: DriverReviewBase, session: Session = Depends(get_session)):
   # Check if driver exists
   driver = session.get(Driver, driver_id)
   if not driver:
       raise HTTPException(status_code=404, detail="Driver not found")

   db_review = DriverReview.model_validate(review, update={"driver_id": driver_id})
   session.add(db_review)
   session.commit()
   session.refresh(db_review)
   return db_review