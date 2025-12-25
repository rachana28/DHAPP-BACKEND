# Driver Hiring Backend

This is a FastAPI backend for a driver hiring application using SQLModel and SQLite.

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

## Running the Application

To run the application with auto-reload:
```
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`

You can view the interactive API documentation at `http://127.0.0.1:8000/docs`

## Database

The application uses SQLite database (`drivers.db`) which will be created automatically on startup.

## API Endpoints

- Drivers: `/drivers`
- Organisations: `/organisations`