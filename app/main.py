from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,
)  # You need to install: pip install apscheduler
from sqlmodel import Session

from app.database import (
    create_db_and_tables,
    engine,
)  # Import engine to create new sessions
from app.routers import (
    auth,
    drivers,
    trips,
    users,
    tow_truck_drivers,
    tow_trips,
    tracking,
)
from app.utils.allocation import process_tier_escalation
from app.utils.tow_allocation import process_tow_tier_escalation


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
    scheduler.add_job(
        run_scheduled_tow_escalation_check, "interval", minutes=1
    )
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
    "http://localhost:5173",  # Vite (Your React App)
    "http://localhost:3000",  # Create React App (Backup)
    "http://127.0.0.1:5173",  # Alternative localhost URL
    "https://dhapp-frontend.onrender.com",  # Render production url
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Allow these specific URLs
    allow_credentials=True,  # Allow cookies/tokens
    allow_methods=["*"],  # Allow all methods (GET, POST, PUT, DELETE)
    allow_headers=["*"],  # Allow all headers
)

app.include_router(auth.router)
app.include_router(drivers.router)
app.include_router(trips.router)
app.include_router(users.router)
app.include_router(tow_truck_drivers.router)
app.include_router(tow_trips.router)
app.include_router(tracking.router)


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
