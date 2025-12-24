from fastapi import FastAPI, Depends, HTTPException
from sqlmodel import SQLModel, Field, create_engine, Session, select
from typing import Optional, List

# -----------------------------
# DATABASE CONFIGURATION
# -----------------------------

DATABASE_URL = "sqlite:///drivers.db"

engine = create_engine(DATABASE_URL, echo=True)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


# -----------------------------
# DATA MODEL (TABLE)
# -----------------------------

class Driver(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str
    license_number: str
    is_available: bool = True


# -----------------------------
# FASTAPI APP
# -----------------------------

app = FastAPI()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# -----------------------------
# DATABASE SESSION DEPENDENCY
# -----------------------------

def get_session():
    with Session(engine) as session:
        yield session


# -----------------------------
# API ROUTES
# -----------------------------

@app.post("/drivers", response_model=Driver)
def create_driver(driver: Driver, session: Session = Depends(get_session)):
    session.add(driver)
    session.commit()
    session.refresh(driver)
    return driver


@app.get("/drivers", response_model=List[Driver])
def get_all_drivers(session: Session = Depends(get_session)):
    drivers = session.exec(select(Driver)).all()
    return drivers


@app.put("/drivers/{driver_id}", response_model=Driver)
def update_driver(
    driver_id: int,
    updated_driver: Driver,
    session: Session = Depends(get_session)
):
    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    driver.name = updated_driver.name
    driver.phone = updated_driver.phone
    driver.license_number = updated_driver.license_number
    driver.is_available = updated_driver.is_available

    session.add(driver)
    session.commit()
    session.refresh(driver)
    return driver


@app.delete("/drivers/{driver_id}")
def delete_driver(driver_id: int, session: Session = Depends(get_session)):
    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    session.delete(driver)
    session.commit()
    return {"message": "Driver deleted successfully"}
