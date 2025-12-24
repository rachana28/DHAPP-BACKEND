from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import List
from app.database import get_session
from app.models import Driver, DriverBase, DriverUpdate

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