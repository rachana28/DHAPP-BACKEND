from fastapi import APIRouter, Form, Depends
from typing import Optional
import redis

from app.core.database import get_redis

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
    vehicle_type: str = Form(..., regex="^(CAR|BIKE)$"),
    user_id: Optional[str] = Form(None),
    # Inject Redis Client
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Calculates towing price based on road distance and time.
    Uses Dynamic Pricing from Redis Config if available.
    """

    # 1. Calculate Distance
    distance_km, duration_min = get_road_distance_duration(
        start_lat, start_lng, dest_lat, dest_lng
    )

    if distance_km is None or distance_km == 0:
        distance_km = 1.0
        duration_min = 10.0

    # 2. Run Intelligent Pricing Algorithm (Pass Redis Client)
    pricing_result = calculate_tow_cost(distance_km, vehicle_type, redis_client)

    # 3. Construct Payload
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
