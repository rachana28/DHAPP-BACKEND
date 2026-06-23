"""Durable-notification persistence for the push-send chokepoint.

Writes inbox rows in its own session so a persistence failure (or commit)
never interferes with the caller's in-flight transaction.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app.core.database import engine
from app.core.models import Notification
from app.modules.notifications.classification import classify

logger = logging.getLogger(__name__)


def persist_for_users(
    user_ids: List[uuid.UUID],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]],
) -> Dict[uuid.UUID, uuid.UUID]:
    """Persist one durable Notification per recipient. Returns a
    ``{user_id: notification_id}`` map (empty when nothing was persisted)."""
    classification = classify(data)
    if classification is None or not user_ids:
        return {}

    category, important = classification
    detail = data.get("detail") if data else None
    payload = dict(data) if data else {}

    id_map: Dict[uuid.UUID, uuid.UUID] = {}
    try:
        with Session(engine) as session:
            for user_id in set(user_ids):
                record = Notification(
                    user_id=user_id,
                    type=category,
                    title=title,
                    body=body or "",
                    detail=detail,
                    data=payload,
                    important=important,
                )
                session.add(record)
                id_map[user_id] = record.id
            session.commit()
    except Exception as e:
        logger.warning("Notification persistence failed: %s", e)
        return {}

    return id_map
