from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.database import create_db_and_tables
from app.routers import drivers, organisations

# This function runs when the server starts
@asynccontextmanager
async def lifespan(app: FastAPI):
   create_db_and_tables() # Automatically creates tables
   yield

app = FastAPI(lifespan=lifespan, title="Driver Hiring Backend")

# Include the routers we created
app.include_router(drivers.router)
app.include_router(organisations.router)

@app.get("/")
def root():
   return {"message": "Welcome to the Driver & Organisation API"}