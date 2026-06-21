"""Connection cap for the AI Diagnostic chat WebSocket.

The chat is a 1:1 conversation between one user and the AI, so there is no room
or broadcast — this manager only bounds how many concurrent sockets a single user
may hold (each open socket can drive Gemini cost). The per-user tally is guarded
by an async lock; the per-connection handler registers on accept and unregisters
in its finally block.
"""

import asyncio
from collections import defaultdict
from typing import Dict, Set

from fastapi import WebSocket

MAX_CONNECTIONS_PER_USER = 2


class AiChatConnectionManager:
    def __init__(self) -> None:
        self._per_user: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, websocket: WebSocket) -> bool:
        """Register a socket. Returns False (without registering) if the per-user
        cap is exceeded; the caller should reject and close."""
        async with self._lock:
            if len(self._per_user[user_id]) >= MAX_CONNECTIONS_PER_USER:
                return False
            self._per_user[user_id].add(websocket)
            return True

    async def disconnect(self, user_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._per_user.get(user_id)
            if sockets:
                sockets.discard(websocket)
                if not sockets:
                    self._per_user.pop(user_id, None)


manager = AiChatConnectionManager()
