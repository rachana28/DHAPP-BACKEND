import os
import time
import boto3
import asyncio
from botocore.exceptions import ClientError
from fastapi import UploadFile, HTTPException

# Fetch R2 credentials from environment
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
ACCESS_KEY_ID = os.getenv("ACCESS_KEY_ID")
SECRET_ACCESS_KEY = os.getenv("SECRET_ACCESS_KEY")
BUCKET_NAME = os.getenv("BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

# Initialize the S3 client pointing to Cloudflare R2
s3_client = boto3.client(
    service_name="s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY_ID,
    aws_secret_access_key=SECRET_ACCESS_KEY,
    region_name="auto",
)

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


async def upload_profile_picture_to_r2(
    file: UploadFile, user_prefix: str, user_id: str
) -> str:
    """
    Validates the file and uploads it non-blockingly to Cloudflare R2.
    """
    if not R2_PUBLIC_URL:
        raise HTTPException(status_code=500, detail="UnExpected Error")

    # 1. Validate File Extension
    file_extension = os.path.splitext(file.filename)[1].lower()
    if file_extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only JPG, PNG, and WEBP are allowed.",
        )

    # 2. Validate File Size safely (Using FastAPI's built-in size attribute)
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413, detail="File size too large. Maximum allowed is 5MB."
        )

    # 3. Generate Unique Filename
    timestamp = int(time.time())
    object_name = (
        f"profile_pictures/{user_prefix}_{user_id}_{timestamp}{file_extension}"
    )

    # 4. Upload to Cloudflare R2 (Non-blocking)
    try:
        # Await the file reading into memory
        file_contents = await file.read()

        # Offload the blocking boto3 network call to a separate thread!
        await asyncio.to_thread(
            s3_client.put_object,
            Bucket=BUCKET_NAME,
            Key=object_name,
            Body=file_contents,
            ContentType=file.content_type,
        )
    except ClientError as e:
        print(f"R2 Upload Error: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to upload image to storage."
        )
    finally:
        # Always close the file pointer to free up memory
        await file.close()

    # 5. Return the Public URL
    return f"{R2_PUBLIC_URL}/{object_name}"
