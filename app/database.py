import os
import ssl
from dotenv import load_dotenv
from sqlmodel import SQLModel, create_engine, Session

# Load environment variables from .env file
load_dotenv()

# PostgreSQL connection details
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_SSL_MODE = os.getenv("DB_SSL_MODE")

if DB_HOST:
    # Use PostgreSQL
    print("Connecting to PostgreSQL database...")
    db_url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    
    connect_args = {}
    if DB_SSL_MODE == 'require':
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connect_args["ssl_context"] = ssl_context

    engine = create_engine(db_url, connect_args=connect_args)
else:
    # Use SQLite for local development
    print("PostgreSQL environment variables not found, connecting to local SQLite database...")
    sqlite_file_name = "drivers.db"
    sqlite_url = f"sqlite:///{sqlite_file_name}"
    engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})


def create_db_and_tables():
    """Creates the tables if they don't exist."""
    SQLModel.metadata.create_all(engine)

def get_session():
    """Dependency to provide a database session for each request."""
    with Session(engine) as session:
        yield session
