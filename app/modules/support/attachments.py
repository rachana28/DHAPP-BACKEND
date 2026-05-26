import os
import uuid
import asyncio
from typing import Dict, Any

from botocore.exceptions import ClientError
from fastapi import UploadFile, HTTPException

from app.utils.storage import (
    s3_client,
    BUCKET_NAME,
    R2_PUBLIC_URL,
    MAX_FILE_SIZE,
    ALLOWED_EXTENSIONS,
    DOC_ALLOWED_EXTENSIONS,
)


def _detect_file_type(extension: str) -> str:
    return "image" if extension in ALLOWED_EXTENSIONS else "document"


async def upload_support_attachment(
    file: UploadFile, ticket_id: int, uploader_prefix: str
) -> Dict[str, Any]:
    """
    Validates an uploaded file (image or document, <= 10MB) and stores it in
    Cloudflare R2 under the ticket's folder. Returns the metadata needed to
    persist a SupportAttachment row.
    """
    if not R2_PUBLIC_URL or not BUCKET_NAME:
        raise HTTPException(status_code=500, detail="Storage not configured")

    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in DOC_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid file type. Only images (JPG, JPEG, PNG, WEBP) and "
                "documents (PDF, DOC, DOCX) are allowed."
            ),
        )

    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File size too large. Maximum allowed is 10MB.",
        )

    file_contents = await file.read()
    actual_size = len(file_contents)
    if actual_size > MAX_FILE_SIZE:
        await file.close()
        raise HTTPException(
            status_code=413,
            detail="File size too large. Maximum allowed is 10MB.",
        )

    safe_uploader = (
        "".join(c for c in uploader_prefix if c.isalnum() or c in "-_")[:32] or "user"
    )
    object_name = (
        f"support_tickets/{ticket_id}/{safe_uploader}_{uuid.uuid4().hex}{extension}"
    )

    try:
        await asyncio.to_thread(
            s3_client.put_object,
            Bucket=BUCKET_NAME,
            Key=object_name,
            Body=file_contents,
            ContentType=file.content_type or "application/octet-stream",
        )
    except ClientError as e:
        print(f"R2 Support Upload Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload attachment.")
    finally:
        await file.close()

    return {
        "r2_key": object_name,
        "file_url": f"{R2_PUBLIC_URL}/{object_name}",
        "file_name": os.path.basename(file.filename),
        "file_type": _detect_file_type(extension),
        "mime_type": file.content_type,
        "file_size": actual_size,
    }
