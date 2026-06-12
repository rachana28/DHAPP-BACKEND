from fastapi import APIRouter, Form, Depends
from typing import Optional
import redis
from sqlmodel import Session

from app.core.database import get_redis, get_session

from app.modules.pricing.pricing_algo import (
    get_road_distance_duration,
    calculate_tow_cost,
    calculate_transport_cost,
    estimate_tow_for_all_types,
    estimate_transport_for_all_types,
    encode_response_data,
    calculate_mechanic_cost,
    DEFAULT_TOW_VEHICLE_TYPE,
    DEFAULT_TRANSPORT_VEHICLE_TYPE,
)

router = APIRouter(prefix="/pricing", tags=["Pricing Calculator"])


@router.post("/calculate-tow")
def calculate_towing_price(
    start_lat: float = Form(...),
    start_lng: float = Form(...),
    dest_lat: float = Form(...),
    dest_lng: float = Form(...),
    tow_vehicle_type: str = Form(DEFAULT_TOW_VEHICLE_TYPE),
    vehicle_type: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Calculates the towing price for ONE selected tow-truck class.
    Rates come from SystemConfig (Redis → DB → default).
    """

    # 1. Calculate Distance
    distance_km, duration_min = get_road_distance_duration(
        start_lat, start_lng, dest_lat, dest_lng
    )

    if distance_km is None or distance_km == 0:
        distance_km = 1.0
        duration_min = 10.0

    # 2. Run per-tow-type pricing
    pricing_result = calculate_tow_cost(
        distance_km, tow_vehicle_type, session, redis_client
    )

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


@router.post("/estimate-tow")
def estimate_towing_price_all_types(
    start_lat: float = Form(...),
    start_lng: float = Form(...),
    dest_lat: float = Form(...),
    dest_lng: float = Form(...),
    user_id: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Returns a price for EVERY supported tow-truck class for the same route, so
    the user app can show the options side-by-side before the customer picks one.
    Plain JSON (not base64-encoded) — clients consume ``options`` directly.
    """
    distance_km, duration_min = get_road_distance_duration(
        start_lat, start_lng, dest_lat, dest_lng
    )
    if distance_km is None or distance_km == 0:
        distance_km = 1.0
        duration_min = 10.0

    options = estimate_tow_for_all_types(distance_km, session, redis_client)

    return {
        "status": "success",
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min),
        "currency": "INR",
        "options": options,
    }


@router.post("/calculate-transport")
def calculate_transport_price(
    start_lat: float = Form(...),
    start_lng: float = Form(...),
    dest_lat: float = Form(...),
    dest_lng: float = Form(...),
    transport_vehicle_type: str = Form(DEFAULT_TRANSPORT_VEHICLE_TYPE),
    vehicle_type: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Calculates the transport price for ONE selected transport class.
    Mirror of /calculate-tow; rates from SystemConfig (Redis → DB → default).
    """
    distance_km, duration_min = get_road_distance_duration(
        start_lat, start_lng, dest_lat, dest_lng
    )

    if distance_km is None or distance_km == 0:
        distance_km = 1.0
        duration_min = 10.0

    pricing_result = calculate_transport_cost(
        distance_km, transport_vehicle_type, session, redis_client
    )

    response_data = {
        "status": "success",
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min),
        "currency": "INR",
        "estimation": pricing_result,
    }

    encoded_payload = encode_response_data(response_data)

    return {"payload": encoded_payload}


@router.post("/estimate-transport")
def estimate_transport_price_all_types(
    start_lat: float = Form(...),
    start_lng: float = Form(...),
    dest_lat: float = Form(...),
    dest_lng: float = Form(...),
    user_id: Optional[str] = Form(None),
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Returns a price for EVERY supported transport class for the same route, so the
    user app can show the options side-by-side after picking the Transport service.
    Plain JSON (not base64-encoded) — clients consume ``options`` directly.
    """
    distance_km, duration_min = get_road_distance_duration(
        start_lat, start_lng, dest_lat, dest_lng
    )
    if distance_km is None or distance_km == 0:
        distance_km = 1.0
        duration_min = 10.0

    options = estimate_transport_for_all_types(distance_km, session, redis_client)

    return {
        "status": "success",
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min),
        "currency": "INR",
        "options": options,
    }


@router.post("/calculate-mechanic")
def calculate_mechanic_price(
    start_lat: float = Form(...),
    start_lng: float = Form(...),
    vehicle_type: str = Form(..., regex="^(CAR|BIKE)$"),
    user_id: Optional[str] = Form(None),
    # Inject Redis Client
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Calculates the estimated mechanic visiting price.
    Uses Dynamic Pricing from Redis Config if available.
    """

    # 1. Run Intelligent Pricing Algorithm
    pricing_result = calculate_mechanic_cost(vehicle_type, redis_client)

    # 2. Construct Payload
    response_data = {
        "status": "success",
        "currency": "INR",
        "estimation": pricing_result,
        # Distance and duration are omitted as the mechanic comes to the user
    }

    # 3. Encode Response exactly like Tow Trucks
    encoded_payload = encode_response_data(response_data)

    return {"payload": encoded_payload}
