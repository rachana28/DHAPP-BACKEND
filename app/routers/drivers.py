from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, func, desc
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


@router.get("/{driver_id}/reviews", response_model=List[DriverReview])
def get_driver_reviews(driver_id: int, session: Session = Depends(get_session)):
   # Check if driver exists
   driver = session.get(Driver, driver_id)
   if not driver:
       raise HTTPException(status_code=404, detail="Driver not found")

   reviews = session.exec(select(DriverReview).where(DriverReview.driver_id == driver_id).order_by(desc(DriverReview.created_at))).all()
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

   # Update driver's average rating
   avg_rating = session.exec(select(func.avg(DriverReview.rating)).where(DriverReview.driver_id == driver_id)).first()
   driver.rating = round(avg_rating, 2) if avg_rating is not None else 0.0
   session.commit()

   return db_review