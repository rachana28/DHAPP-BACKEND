from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,
)  # You need to install: pip install apscheduler
from sqlmodel import Session
from fastapi_limiter import FastAPILimiter
import redis.asyncio as redis_async
import os

from app.core.database import (
    create_db_and_tables,
    engine,
)
from app.modules.config import router as config_router

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
from app.modules.admin import (
    router as admin_router,
    support_router as admin_support_router,
)
from app.modules.support import (
    router as support_router,
    ws_router as support_ws_router,
)
from app.modules.support.scheduler_jobs import (
    auto_close_inactive_support_tickets,
    cleanup_orphan_support_attachments,
)
from app.modules.mechanic import (
    trip_router as mechanic_router,
    profile_router as mechanic_profile_router,
)
from app.modules.service import (
    center_router as service_center_router,
    user_router as service_user_router,
)

# Import Services for Scheduled Tasks
from app.modules.trips.allocation import process_tier_escalation
from app.modules.towing.tow_allocation import process_tow_tier_escalation
from app.modules.trips.scheduler_jobs import (
    generate_otp_for_trip_scheduler,
    expire_otp_for_trip_scheduler,
    auto_end_trip_scheduler,
    driver_payment_timeout_scheduler,
    daily_settlement_scheduler,
    auto_resolve_paused_trips_scheduler,
)


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

    # --- ASYNC REDIS CONNECTION WITH AUTHENTICATION ---
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    redis_password = os.getenv("REDIS_PASSWORD")

    redis_connection = redis_async.Redis(
        host=redis_host,
        port=redis_port,
        username="default",
        password=redis_password,
        db=0,
        decode_responses=True,
    )

    await FastAPILimiter.init(redis_connection)
    # ----------------------------------------------------------

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_scheduled_escalation_check, "interval", minutes=1)
    scheduler.add_job(run_scheduled_tow_escalation_check, "interval", minutes=1)
    # Trip OTP / payment / billing automation
    scheduler.add_job(generate_otp_for_trip_scheduler, "interval", minutes=1)
    scheduler.add_job(expire_otp_for_trip_scheduler, "interval", minutes=5)
    scheduler.add_job(auto_end_trip_scheduler, "interval", minutes=2)
    scheduler.add_job(driver_payment_timeout_scheduler, "interval", minutes=1)
    scheduler.add_job(daily_settlement_scheduler, "cron", hour=23, minute=59)
    # Resolve trips stuck in `paused`: advance/full upfront unpaid > 1h
    # (auto-convert to trip_day) and trip_day daily bill unpaid > 48h
    # (force-close the booking with a final settlement).
    scheduler.add_job(auto_resolve_paused_trips_scheduler, "interval", minutes=5)
    # Auto-close service-linked support tickets after 2hrs of inactivity
    scheduler.add_job(auto_close_inactive_support_tickets, "interval", minutes=10)
    # Clean up attachments uploaded but never linked to a message
    scheduler.add_job(cleanup_orphan_support_attachments, "interval", minutes=30)
    scheduler.start()
    print("🚀 Scheduler started.")

    yield

    scheduler.shutdown()
    print("🛑 Scheduler shut down.")
    await redis_connection.close()


app = FastAPI(lifespan=lifespan, title="Driver Hiring Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router.router)
app.include_router(auth_router.router)
app.include_router(drivers_router.router)
app.include_router(trips_router.router)
app.include_router(users_router.router)
app.include_router(tow_drivers_router.router)
app.include_router(tow_trips_router.router)
app.include_router(tracking_router.router)
app.include_router(pricing_router.router)
app.include_router(support_router.router)
app.include_router(support_ws_router.router)
# Admin-only support endpoints (tickets + FAQ CRUD) live in app.modules.admin
app.include_router(admin_support_router.router)
app.include_router(admin_support_router.faq_router)
app.include_router(config_router.router)
app.include_router(mechanic_router.router)
app.include_router(mechanic_profile_router.router)
app.include_router(service_center_router.router)
app.include_router(service_user_router.router)


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
