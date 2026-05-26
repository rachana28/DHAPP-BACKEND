import asyncio
import json
from collections import defaultdict
from typing import Dict, Set, Optional, Any

from fastapi import WebSocket

# Per-user-per-ticket cap. A handful of devices is normal (phone + tablet
# for a user, multiple browser tabs for an admin); 5 covers them without
# allowing runaway clients to flood memory.
MAX_CONNECTIONS_PER_KEY = 5


class SupportConnectionManager:
    def __init__(self) -> None:
        # ticket_id -> role -> set[WebSocket]
        self._rooms: Dict[int, Dict[str, Set[WebSocket]]] = defaultdict(
            lambda: defaultdict(set)
        )
        # (ticket_id, sender_id) -> set[WebSocket]: per-identity tally
        self._per_identity: Dict[tuple, Set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(
        self,
        ticket_id: int,
        role: str,
        websocket: WebSocket,
        identity: Optional[str] = None,
    ) -> bool:
        """
        Register a WebSocket. Returns False (and does NOT register) if
        the per-identity cap is exceeded. Caller should close the socket.
        """
        async with self._lock:
            key = (ticket_id, identity or role)
            if len(self._per_identity[key]) >= MAX_CONNECTIONS_PER_KEY:
                return False
            self._rooms[ticket_id][role].add(websocket)
            self._per_identity[key].add(websocket)
            return True

    async def disconnect(
        self,
        ticket_id: int,
        role: str,
        websocket: WebSocket,
        identity: Optional[str] = None,
    ) -> None:
        async with self._lock:
            sockets = self._rooms.get(ticket_id, {}).get(role)
            if sockets and websocket in sockets:
                sockets.remove(websocket)
            # Cleanup empty buckets
            if ticket_id in self._rooms and not self._rooms[ticket_id].get(role):
                self._rooms[ticket_id].pop(role, None)
            if ticket_id in self._rooms and not self._rooms[ticket_id]:
                self._rooms.pop(ticket_id, None)
            # Per-identity bookkeeping
            key = (ticket_id, identity or role)
            ident_set = self._per_identity.get(key)
            if ident_set:
                ident_set.discard(websocket)
                if not ident_set:
                    self._per_identity.pop(key, None)

    def is_role_online(self, ticket_id: int, role: str) -> bool:
        return bool(self._rooms.get(ticket_id, {}).get(role))

    def is_any_customer_online(self, ticket_id: int) -> bool:
        """Returns True if any non-admin role (user/driver/etc.) is connected."""
        room = self._rooms.get(ticket_id, {})
        for r, sockets in room.items():
            if r != "admin" and sockets:
                return True
        return False

    async def broadcast(
        self,
        ticket_id: int,
        payload: Dict[str, Any],
        exclude_ws: Optional[WebSocket] = None,
    ) -> None:
        """Send payload to all sockets in the room."""
        text = json.dumps(payload, default=str)
        targets = []
        room = self._rooms.get(ticket_id, {})
        for sockets in room.values():
            for ws in list(sockets):
                if ws is exclude_ws:
                    continue
                targets.append(ws)
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                # Ignore broken sockets here; the per-connection handler will
                # detect the disconnect and unregister.
                pass

    async def close_room(self, ticket_id: int, reason: str) -> None:
        """Send a final 'closed' frame and close all sockets in this ticket's room."""
        text = json.dumps({"type": "closed", "reason": reason})
        async with self._lock:
            room = self._rooms.pop(ticket_id, None)
            # Drop any per-identity entries for this ticket
            for key in [k for k in self._per_identity if k[0] == ticket_id]:
                self._per_identity.pop(key, None)
        if not room:
            return
        for sockets in room.values():
            for ws in list(sockets):
                try:
                    await ws.send_text(text)
                except Exception:
                    pass
                try:
                    await ws.close()
                except Exception:
                    pass


# Module-level singleton (single-process)
manager = SupportConnectionManager()
