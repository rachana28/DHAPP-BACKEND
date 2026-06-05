import math
import requests
from datetime import datetime
import json
import base64
from typing import List, Optional

from sqlmodel import Session

from app.modules.trips.pricing_calculator import _get_config_value

# --- CONFIGURATION ---
OSRM_BASE_URL = "http://router.project-osrm.org/route/v1/driving"

TOW_VEHICLE_TYPES = ("flatbed", "wheel_lift", "hook_chain", "integrated")
DEFAULT_TOW_VEHICLE_TYPE = "wheel_lift"

DEFAULT_TOW_PRICING = {
    "flatbed": {"base_fare": 600.0, "per_km": 40.0, "min_charge": 800.0},
    "wheel_lift": {"base_fare": 450.0, "per_km": 30.0, "min_charge": 600.0},
    "hook_chain": {"base_fare": 350.0, "per_km": 25.0, "min_charge": 500.0},
    "integrated": {"base_fare": 1200.0, "per_km": 60.0, "min_charge": 2000.0},
}


def get_road_distance_duration(
    start_lat: float, start_lng: float, dest_lat: float, dest_lng: float
):
    """
    Fetches accurate road distance and duration using OSRM.
    """
    try:
        url = f"{OSRM_BASE_URL}/{start_lng},{start_lat};{dest_lng},{dest_lat}?overview=false"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("routes"):
                route = data["routes"][0]
                distance_meters = route["distance"]
                duration_seconds = route["duration"]
                return (
                    distance_meters / 1000.0,
                    duration_seconds / 60.0,
                )
    except Exception as e:
        print(f"Routing Error: {e}")

    # Fallback: Haversine
    R = 6371
    dLat = math.radians(dest_lat - start_lat)
    dLon = math.radians(dest_lng - start_lng)
    a = math.sin(dLat / 2) * math.sin(dLat / 2) + math.cos(
        math.radians(start_lat)
    ) * math.cos(math.radians(dest_lat)) * math.sin(dLon / 2) * math.sin(dLon / 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_km = R * c * 1.4
    return dist_km, dist_km * 3


def normalize_tow_vehicle_type(tow_vehicle_type: Optional[str]) -> str:
    """Coerce to a supported tow-truck class, defaulting unknown/empty values."""
    t = (tow_vehicle_type or "").strip().lower()
    return t if t in DEFAULT_TOW_PRICING else DEFAULT_TOW_VEHICLE_TYPE


def _tow_cfg(
    session: Optional[Session], redis_client, key: str, default: float
) -> float:
    """SystemConfig lookup (Redis → DB → default). Safe when session is None."""
    if session is None:
        return float(default)
    return _get_config_value(session, redis_client, key, default)


def calculate_tow_cost(
    distance_km: float,
    tow_vehicle_type: str,
    session: Optional[Session] = None,
    redis_client: Optional[object] = None,
) -> dict:
    """Per-tow-type tow fare. Rates come from SystemConfig (Redis → DB → default).

    ``tow_vehicle_type`` is the tow-TRUCK class (flatbed / wheel_lift / hook_chain
    / integrated) — NOT the customer's vehicle. Keys:
    ``pricing_tow_<type>_base_fare`` / ``_per_km`` / ``_min_charge`` plus the
    evening/night multipliers. Unknown/empty types fall back to a default class.
    """
    tow_type = normalize_tow_vehicle_type(tow_vehicle_type)
    defaults = DEFAULT_TOW_PRICING[tow_type]

    base_fare = _tow_cfg(
        session,
        redis_client,
        f"pricing_tow_{tow_type}_base_fare",
        defaults["base_fare"],
    )
    rate_per_km = _tow_cfg(
        session, redis_client, f"pricing_tow_{tow_type}_per_km", defaults["per_km"]
    )
    min_charge = _tow_cfg(
        session,
        redis_client,
        f"pricing_tow_{tow_type}_min_charge",
        defaults["min_charge"],
    )

    # Time multiplier (admin-tunable surcharge for evening / night).
    current_hour = datetime.now().hour
    night_mult = _tow_cfg(session, redis_client, "pricing_tow_night_multiplier", 1.5)
    evening_mult = _tow_cfg(
        session, redis_client, "pricing_tow_evening_multiplier", 1.25
    )
    if 22 <= current_hour or current_hour < 5:
        time_multiplier, time_label = night_mult, "Night"
    elif 18 <= current_hour < 22:
        time_multiplier, time_label = evening_mult, "Evening"
    else:
        time_multiplier, time_label = 1.0, "Day"

    # Distance tiering (tapering per-km rate beyond 10 / 50 km).
    distance_km = max(0.0, float(distance_km or 0.0))
    if distance_km <= 10:
        distance_cost = distance_km * rate_per_km
    elif distance_km <= 50:
        distance_cost = (10 * rate_per_km) + ((distance_km - 10) * (rate_per_km * 0.9))
    else:
        distance_cost = (
            (10 * rate_per_km)
            + (40 * (rate_per_km * 0.9))
            + ((distance_km - 50) * (rate_per_km * 0.85))
        )

    sub_total = base_fare + distance_cost
    total_price = max(sub_total * time_multiplier, min_charge)
    final_price = math.ceil(total_price / 10.0) * 10

    return {
        "final_price": final_price,
        "breakdown": {
            "tow_vehicle_type": tow_type,
            "base_fare": base_fare,
            "distance_km": round(distance_km, 2),
            "distance_cost": round(distance_cost, 2),
            "rate_per_km": rate_per_km,
            "time_multiplier": time_multiplier,
            "time_slot": time_label,
            "min_charge": min_charge,
        },
    }


def estimate_tow_for_all_types(
    distance_km: float,
    session: Optional[Session] = None,
    redis_client: Optional[object] = None,
) -> List[dict]:
    """Price every supported tow-truck class for a given distance, so the user
    app can present the options side-by-side."""
    return [
        {
            "tow_vehicle_type": t,
            **calculate_tow_cost(distance_km, t, session, redis_client),
        }
        for t in TOW_VEHICLE_TYPES
    ]


def calculate_mechanic_cost(
    vehicle_type: str, redis_client: Optional[object] = None
) -> dict:
    """
    Pricing Algorithm for Mechanic Services.
    Fetches dynamic visiting charge from Redis if available.
    """
    current_hour = datetime.now().hour

    # 1. Base Parameters (Defaults for Visiting Charge)
    if vehicle_type.upper() == "BIKE":
        base_visit_charge = 150.0
    else:  # CAR / SUV
        base_visit_charge = 300.0

    # 2. Dynamic Override (Check Admin Config)
    if redis_client:
        try:
            v_key = vehicle_type.lower()
            # Look for a specific mechanic config, e.g., config:mechanic_bike_base
            dyn_base = redis_client.get(f"config:mechanic_{v_key}_base")
            if dyn_base:
                base_visit_charge = float(dyn_base)
        except Exception as e:
            print(f"Mechanic Pricing Config Fetch Error: {e}")

    # 3. Time Multiplier (AI Heuristic - mirrors Tow Truck logic)
    if 22 <= current_hour or current_hour < 5:
        time_multiplier = 1.5  # Night
        time_label = "Night"
    elif 18 <= current_hour < 22:
        time_multiplier = 1.25  # Evening
        time_label = "Evening"
    else:
        time_multiplier = 1.0  # Day
        time_label = "Day"

    # 4. Final Calculation
    total_price = base_visit_charge * time_multiplier

    # Round to nearest 10
    final_price = math.ceil(total_price / 10.0) * 10

    return {
        "final_price": final_price,
        "breakdown": {
            "base_visit_charge": base_visit_charge,
            "time_multiplier": time_multiplier,
            "time_slot": time_label,
            "vehicle_type": vehicle_type,
        },
    }


def encode_response_data(data: dict) -> str:
    json_str = json.dumps(data)
    encoded_bytes = base64.b64encode(json_str.encode("utf-8"))
    return encoded_bytes.decode("utf-8")
