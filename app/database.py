from sqlmodel import SQLModel, create_engine, Session

# This connects to the SQLite file in the parent directory
sqlite_file_name = "drivers.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# check_same_thread=False is needed for SQLite with FastAPI
engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})

def create_db_and_tables():
   """Creates the tables if they don't exist."""
   SQLModel.metadata.create_all(engine)

def get_session():
   """Dependency to provide a database session for each request."""
   with Session(engine) as session:
       yield session