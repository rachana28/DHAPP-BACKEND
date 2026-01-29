from fastapi import APIRouter, Form
from typing import Optional
from app.modules.pricing.pricing_algo import (
    get_road_distance_duration,
    calculate_tow_cost,
    encode_response_data,
)

router = APIRouter(prefix="/pricing", tags=["Pricing Calculator"])


@router.post("/calculate-tow")
def calculate_towing_price(
    start_lat: float = Form(...),
    start_lng: float = Form(...),
    dest_lat: float = Form(...),
    dest_lng: float = Form(...),
    vehicle_type: str = Form(..., regex="^(CAR|BIKE)$"),  # Strict Regex Validation
    user_id: Optional[str] = Form(None),  # Optional for tracking/logging later
):
    """
    Calculates towing price based on road distance and time.
    Payload: Form Data (Harder to automate/inject json)
    Response: Base64 Encoded JSON (Obfuscated)
    """

    # 1. Calculate Distance (Road Routing)
    distance_km, duration_min = get_road_distance_duration(
        start_lat, start_lng, dest_lat, dest_lng
    )

    if distance_km is None or distance_km == 0:
        # Fallback logic handled in utils, but if 0
        distance_km = 1.0  # Minimum distance
        duration_min = 10.0

    # 2. Run Intelligent Pricing Algorithm
    pricing_result = calculate_tow_cost(distance_km, vehicle_type)

    # 3. Construct Final Data Payload
    response_data = {
        "status": "success",
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min),
        "currency": "INR",
        "estimation": pricing_result,
    }

    # 4. Encode Response
    encoded_payload = encode_response_data(response_data)

    return {"payload": encoded_payload}
