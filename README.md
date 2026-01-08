# Driver Hiring Backend

This is a FastAPI backend for a driver hiring application using SQLModel and PostgreSQL.

## Setup

1. Clone the repository and navigate to the project directory.

2. Create a virtual environment:
   ```
   python -m venv venv
   ```

3. Activate the virtual environment:
   - On Windows: `.\venv\Scripts\Activate.ps1`
   - On macOS/Linux: `source venv/bin/activate`

4. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

5. **Environment Variables (Optional)**

   To connect to a PostgreSQL database (e.g., in production), create a `.env` file in the root of the `DHAPP-BACKEND` directory with the following variables:

   ```env
   DB_HOST=your_database_host
   DB_PORT=5432
   DB_NAME=your_database_name
   DB_USER=your_username
   DB_PASSWORD=your_password
   DB_SSL_MODE=require
   ```

   If these variables are not present, the application will automatically use a local SQLite database (`drivers.db`) for development.

## Running the Application

To run the application with auto-reload:
```
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`

You can view the interactive API documentation at `http://127.0.0.1:8000/docs`

## Database

The application is configured to work with two types of databases:
- **PostgreSQL**: Used in production or when a `DATABASE_URL` is provided in the environment.
- **SQLite**: Used for local development as a fallback if no `DATABASE_URL` is found. A `drivers.db` file will be created automatically in the project root.

The necessary tables will be created automatically on startup in the configured database.

## API Endpoints

- Drivers: `/drivers`
- Organisations: `/organisations`