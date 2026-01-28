import math
import requests
from datetime import datetime
import json
import base64

# --- CONFIGURATION ---
OSRM_BASE_URL = "http://router.project-osrm.org/route/v1/driving"


def get_road_distance_duration(
    start_lat: float, start_lng: float, dest_lat: float, dest_lng: float
):
    """
    Fetches accurate road distance and duration using OSRM (Open Source Routing Machine).
    Fallback to Haversine if OSRM fails.
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
                )  # Returns km, min
    except Exception as e:
        print(f"Routing Error: {e}")

    # Fallback: Haversine Formula * 1.4 (Tortuosity factor for road approx)
    R = 6371  # Earth radius in km
    dLat = math.radians(dest_lat - start_lat)
    dLon = math.radians(dest_lng - start_lng)
    a = math.sin(dLat / 2) * math.sin(dLat / 2) + math.cos(
        math.radians(start_lat)
    ) * math.cos(math.radians(dest_lat)) * math.sin(dLon / 2) * math.sin(dLon / 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_km = R * c * 1.4
    return dist_km, dist_km * 3  # Approx 3 mins per km


def calculate_tow_cost(distance_km: float, vehicle_type: str) -> dict:
    """
    Intelligent Pricing Algorithm simulating a trained model logic.
    Factors: Base Fare, Distance Tiers, Time of Day, Vehicle Type.
    """
    current_hour = datetime.now().hour

    # 1. Base Parameters
    if vehicle_type.upper() == "BIKE":
        base_fare = 255
        rate_per_km = 12
        min_charge = 380.0
    else:  # CAR / SUV
        base_fare = 450
        rate_per_km = 15
        min_charge = 580.0

    # 2. Time Multiplier (AI Heuristic)
    # Night (10 PM - 5 AM): High Risk/Traffic Free but scarce drivers
    # Evening (6 PM - 10 PM): High Traffic
    # Day (5 AM - 6 PM): Standard
    if 22 <= current_hour or current_hour < 5:
        time_multiplier = 1.5  # Night Surge
        time_label = "Night"
    elif 18 <= current_hour < 22:
        time_multiplier = 1.25  # Peak Traffic
        time_label = "Evening"
    else:
        time_multiplier = 1.0  # Standard
        time_label = "Day"

    # 3. Distance Tiering (Longer distance = slightly lower marginal cost)
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

    # 4. Final Calculation
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
        },
    }


def encode_response_data(data: dict) -> str:
    """
    Base64 encodes the JSON response to obfuscate pricing logic from casual UI inspection.
    """
    json_str = json.dumps(data)
    encoded_bytes = base64.b64encode(json_str.encode("utf-8"))
    return encoded_bytes.decode("utf-8")
