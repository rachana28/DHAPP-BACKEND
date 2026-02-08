import math
import requests
from datetime import datetime
import json
import base64
from typing import Optional

# --- CONFIGURATION ---
OSRM_BASE_URL = "http://router.project-osrm.org/route/v1/driving"


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


def calculate_tow_cost(
    distance_km: float, vehicle_type: str, redis_client: Optional[object] = None
) -> dict:
    """
    Intelligent Pricing Algorithm.
    Fetches dynamic base fare from Redis if available.
    """
    current_hour = datetime.now().hour

    # 1. Base Parameters (Defaults)
    if vehicle_type.upper() == "BIKE":
        base_fare = 255.0
        rate_per_km = 12.0
        min_charge = 380.0
    else:  # CAR / SUV
        base_fare = 450.0
        rate_per_km = 15.0
        min_charge = 580.0

    # 2. Dynamic Override (Check Admin Config)
    if redis_client:
        try:
            # Keys must match what Admin sets in System Config
            # Example Key: config:bike_base_fare
            v_key = vehicle_type.lower()

            # Fetch Base Fare
            dyn_base = redis_client.get(f"config:{v_key}_base_fare")
            if dyn_base:
                base_fare = float(dyn_base)

            # Fetch Rate Per KM
            dyn_rate = redis_client.get(f"config:{v_key}_rate_per_km")
            if dyn_rate:
                rate_per_km = float(dyn_rate)

            # Fetch Min Charge
            dyn_min = redis_client.get(f"config:{v_key}_min_charge")
            if dyn_min:
                min_charge = float(dyn_min)

        except Exception as e:
            print(f"Pricing Config Fetch Error: {e}")

    # 3. Time Multiplier (AI Heuristic)
    if 22 <= current_hour or current_hour < 5:
        time_multiplier = 1.5  # Night
        time_label = "Night"
    elif 18 <= current_hour < 22:
        time_multiplier = 1.25  # Evening
        time_label = "Evening"
    else:
        time_multiplier = 1.0  # Day
        time_label = "Day"

    # 4. Distance Calculation
    distance_cost = 0.0
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

    # 5. Final Calculation
    sub_total = base_fare + distance_cost
    total_price = max(sub_total * time_multiplier, min_charge)

    # Round to nearest 10
    final_price = math.ceil(total_price / 10.0) * 10

    return {
        "final_price": final_price,
        "breakdown": {
            "base_fare": base_fare,
            "distance_cost": round(distance_cost, 2),
            "time_multiplier": time_multiplier,
            "time_slot": time_label,
            "vehicle_type": vehicle_type,
            "rate_used": rate_per_km,
        },
    }


def encode_response_data(data: dict) -> str:
    json_str = json.dumps(data)
    encoded_bytes = base64.b64encode(json_str.encode("utf-8"))
    return encoded_bytes.decode("utf-8")
