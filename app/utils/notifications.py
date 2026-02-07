import requests
from typing import List, Dict, Any
from sqlmodel import Session, select
from app.core.models import UserDevice

# Expo Push API URL
EXPO_URL = "https://exp.host/--/api/v2/push/send"


def send_push_notification(
    session: Session,
    user_ids: List[int],
    title: str,
    body: str,
    data: Dict[str, Any] = None,
):
    """
    Sends notifications to all devices belonging to the list of user_ids.
    Automatically removes invalid/dead tokens (Fix Flaw 4).
    """
    # 1. Fetch all devices for these users
    statement = select(UserDevice).where(UserDevice.user_id.in_(user_ids))
    devices = session.exec(statement).all()

    if not devices:
        return

    # 2. Prepare Messages (Expo allows batches of 100)
    messages = []
    token_map = {}  # Map token -> device_obj for easy deletion later

    for device in devices:
        if not device.token.startswith("ExponentPushToken"):
            continue

        token_map[device.token] = device
        messages.append(
            {
                "to": device.token,
                "title": title,
                "body": body,
                "data": data or {},
                "sound": "default",
            }
        )

    if not messages:
        return

    # 3. Send to Expo
    try:
        response = requests.post(
            EXPO_URL,
            json=messages,
            headers={
                "Accept": "application/json",
                "Accept-encoding": "gzip, deflate",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get("data", [])

        # 4. Handle Errors & Dead Tokens
        dirty = False
        for i, result in enumerate(results):
            if result["status"] == "error":
                error_code = result["details"].get("error")
                # "DeviceNotRegistered" means app was uninstalled
                if error_code == "DeviceNotRegistered":
                    token_to_remove = messages[i]["to"]
                    device_to_delete = token_map.get(token_to_remove)
                    if device_to_delete:
                        print(f"Removing dead token: {token_to_remove}")
                        session.delete(device_to_delete)
                        dirty = True

        if dirty:
            session.commit()

    except Exception as e:
        print(f"Notification System Error: {e}")
