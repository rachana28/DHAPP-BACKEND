from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,
)  # You need to install: pip install apscheduler
from sqlmodel import Session

from app.core.database import (
    create_db_and_tables,
    engine,
)

# Import Routers from Modules
from app.modules.auth import router as auth_router, users as users_router
from app.modules.drivers import router as drivers_router
from app.modules.trips import router as trips_router
from app.modules.towing import (
    driver_router as tow_drivers_router,
    trip_router as tow_trips_router,
)
from app.modules.pricing import router as pricing_router
from app.modules.tracking import router as tracking_router

# Import Services for Scheduled Tasks
from app.modules.trips.allocation import process_tier_escalation
from app.modules.towing.tow_allocation import process_tow_tier_escalation


def run_scheduled_escalation_check():
    """
    This function runs every minute.
    It creates a NEW database session specifically for this task.
    """
    print("⏳ Running scheduled escalation check...")
    with Session(engine) as session:
        try:
            count = process_tier_escalation(session)
            if count > 0:
                print(f"✅ Escalated {count} trips to next tier.")
        except Exception as e:
            print(f"❌ Error in scheduled task: {e}")


def run_scheduled_tow_escalation_check():
    """Runs every minute for tow trips."""
    print("⏳ Running scheduled TOW escalation check...")
    with Session(engine) as session:
        try:
            count = process_tow_tier_escalation(session)
            if count > 0:
                print(f"✅ Escalated {count} tow trips.")
        except Exception as e:
            print(f"❌ Error in tow scheduled task: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_scheduled_escalation_check, "interval", minutes=1)
    scheduler.add_job(run_scheduled_tow_escalation_check, "interval", minutes=1)
    scheduler.start()
    print("🚀 Scheduler started.")

    yield

    scheduler.shutdown()
    print("🛑 Scheduler shut down.")


app = FastAPI(lifespan=lifespan, title="Driver Hiring Backend")

# --- Static File Serving ---
# This will serve files from the 'media' directory at the '/media' URL path
app.mount("/media", StaticFiles(directory="media"), name="media")


origins = [
    # 1. Production Web Frontend (User & Driver)
    "https://dhapp-frontend.onrender.com",
    "https://dhire-driverspace.onrender.com",

    # 2. Local Web Development
    "http://localhost:5173",
    "http://localhost:3000",
]

# DYNAMIC ORIGIN REGEX
# This Logic:
# 1. ^http://localhost...        -> Trusts the machine itself (Simulators)
# 2. ^http://127\.0\.0\.1...     -> Trusts the loopback IP (Android Emulators)
# 3. ^http://192\.168\....       -> Trusts YOUR WIFI NETWORK (iPhone + Laptop)
# 4. ^http://10\....             -> Trusts Enterprise/School Networks
# 5. ^http://172\....            -> Trusts Docker/WSL Networks
# 6. ^https://.*\.onrender\.com$ -> Trusts your cloud deployments

allow_origin_regex = (
    r"^(http://localhost(:\d+)?)|"
    r"(http://127\.0\.0\.1(:\d+)?)|"
    r"(http://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?)|" 
    r"(http://10\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?)|"
    r"(http://172\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?)|"
    r"(https://.*\.onrender\.com)$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,             # Explicit allow list (Web)
    allow_origin_regex=allow_origin_regex, # Smart Regex (Mobile/Dev)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(drivers_router.router)
app.include_router(trips_router.router)
app.include_router(users_router.router)
app.include_router(tow_drivers_router.router)
app.include_router(tow_trips_router.router)
app.include_router(tracking_router.router)
app.include_router(pricing_router.router)


@app.get("/")
def root():
    return {"message": "Welcome to the Driver & Organisation API"}


@app.get("/create-tables")
def create_tables_endpoint():
    """
    Manually trigger the creation of database tables.
    This is a temporary endpoint for local development.
    """
    try:
        create_db_and_tables()
        return {"message": "Database tables created successfully."}
    except Exception as e:
        return {"message": f"Error creating tables: {e}"}
