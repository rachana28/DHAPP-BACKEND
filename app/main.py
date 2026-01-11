from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware  # <--- 1. Import this

from app.database import create_db_and_tables
from app.routers import auth, drivers, organisations, trips

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(lifespan=lifespan, title="Driver Hiring Backend")

origins = [
    "http://localhost:5173",  # Vite (Your React App)
    "http://localhost:3000",  # Create React App (Backup)
    "http://127.0.0.1:5173",  # Alternative localhost URL
    "https://dhapp-frontend.onrender.com" #Render production url
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
app.include_router(organisations.router)
app.include_router(trips.router)


@app.get("/")
def root():
    return {"message": "Welcome to the Driver & Organisation API"}
