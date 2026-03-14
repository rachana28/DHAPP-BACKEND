# 1. Matched to Python 3.11 (same as your GitHub Actions pipeline)
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# 2. Tell the container it will use a port
EXPOSE 8000

# 3. Use Render's dynamic $PORT variable (with 8000 as a fallback)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]