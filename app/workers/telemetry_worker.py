"""Async MQTT telemetry worker for live tow / mechanic ride tracking.

Subscribes to ``rides/+/telemetry`` and, for every incoming GPS payload:

1. **Bridges to Redis** using the exact keys the existing ``/tracking/ws``
   WebSocket reads (``loc:{user_id}`` and ``loc:trip:{kind}:{ref}``), so the
   current user-facing live-tracking keeps working unchanged.
2. **Buffers** the latest point per booking in memory (last-write-wins).
3. Runs **throttled geofence checks**: on pickup arrival it flips the booking to
   ``arrived`` and auto-generates the start/end OTP (pushed to the user); for a
   tow already ``in_progress`` it flips to ``near_destination`` at the drop-off
   and flags payment due.
4. Every 30s **batch-upserts** each buffered provider's ``current_location``
   (plain lat/lng columns) to Postgres — bounding DB writes regardless of GPS
   frequency.

Design notes
------------
- The app's SQLAlchemy engine is synchronous (pg8000), so all DB work runs in a
  thread via ``asyncio.to_thread`` to avoid blocking the event loop.
- If ``MQTT_HOST`` is unset or ``aiomqtt`` is unavailable (e.g. local dev with no
  broker), the worker logs a warning and no-ops — bookings still function via the
  existing offer/accept flow and the HTTP ``/tracking/update`` fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from typing import Dict, Optional, Tuple

from sqlmodel import Session

from app.core.database import engine
from app.core.models import MechanicTrip, TowTrip, TowTruckDriver, Mechanic
from app.modules.dispatch import geo
from app.modules.trips import booking_otp_service
from app.utils.id_generator import get_by_reference
from app.utils.notifications import send_push_notification
from app.utils.time_utils import now_ist
from app.workers.topics import TELEMETRY_WILDCARD, parse_telemetry_topic

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 30  # batch-upsert cadence (spec: once every 30 seconds)
GEOFENCE_CHECK_EVERY_S = 5.0  # per-booking cooldown between geofence evaluations
LOC_TTL_S = 300  # Redis live-location TTL (matches /tracking/update)
RECONNECT_BACKOFF_S = 5


class TelemetryWorker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        # ref -> (lat, lng) latest buffered point awaiting the 30s flush.
        self._buffer: Dict[str, Tuple[float, float]] = {}
        self._geofence_cooldown: Dict[str, float] = {}
        # ref -> (kind, customer_user_id, provider_user_id). Owner is fixed for
        # a booking's life, so cache it to avoid a DB hit on every GPS ping.
        self._owner_cache: Dict[str, Tuple] = {}
        self._redis = None  # async redis client (lazy)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        host = os.getenv("MQTT_HOST")
        if not host:
            logger.warning("MQTT_HOST not set — telemetry worker disabled.")
            return
        try:
            import aiomqtt  # noqa: F401
        except Exception as e:  # pragma: no cover
            logger.warning("aiomqtt unavailable (%s) — telemetry worker disabled.", e)
            return

        self._init_redis()
        flush_task = asyncio.create_task(self._flush_loop())
        try:
            await self._consume_loop()
        finally:
            self._stop.set()
            flush_task.cancel()
            await self._flush()  # final drain
            if self._redis is not None:
                try:
                    await self._redis.close()
                except Exception:
                    pass

    def request_stop(self) -> None:
        self._stop.set()

    def _init_redis(self) -> None:
        try:
            import redis.asyncio as redis_async

            self._redis = redis_async.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                username="default",
                password=os.getenv("REDIS_PASSWORD"),
                db=0,
                decode_responses=True,
            )
        except Exception as e:
            logger.warning("Telemetry worker Redis init failed (%s).", e)
            self._redis = None

    # ── MQTT consume ─────────────────────────────────────────────────────────
    async def _consume_loop(self) -> None:
        import aiomqtt

        tls_context = None
        if os.getenv("MQTT_TLS", "").lower() in ("1", "true", "yes"):
            tls_context = ssl.create_default_context()

        client_kwargs = dict(
            hostname=os.getenv("MQTT_HOST"),
            port=int(os.getenv("MQTT_PORT", 1883)),
            username=os.getenv("MQTT_USERNAME") or None,
            password=os.getenv("MQTT_PASSWORD") or None,
            tls_context=tls_context,
        )

        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(**client_kwargs) as client:
                    await client.subscribe(TELEMETRY_WILDCARD)
                    logger.info("Telemetry worker subscribed to %s", TELEMETRY_WILDCARD)
                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        await self._handle_message(str(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telemetry MQTT connection error: %s — retrying.", e)
                await asyncio.sleep(RECONNECT_BACKOFF_S)

    async def _handle_message(self, topic: str, payload) -> None:
        ref = parse_telemetry_topic(topic)
        if not ref:
            return
        try:
            raw = (
                payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
            )
            data = json.loads(raw)
            lat = float(data["lat"])
            lng = float(data["lng"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return  # ignore malformed payloads

        # 1) bridge to Redis for the existing WebSocket read path
        await self._bridge_to_redis(ref, lat, lng, data)
        # 2) buffer for the 30s batch upsert
        self._buffer[ref] = (lat, lng)
        # 3) throttled geofence evaluation
        await self._maybe_check_geofence(ref, lat, lng)

    async def _bridge_to_redis(self, ref: str, lat: float, lng: float, data) -> None:
        if self._redis is None:
            return
        try:
            owner = self._owner_cache.get(ref)
            if owner is None:
                owner = await asyncio.to_thread(self._resolve_owner, ref)
                if owner[0]:  # only cache once a provider is assigned
                    self._owner_cache[ref] = owner
            kind, user_id, provider_user_id = owner
            if not kind:
                return
            blob = json.dumps(
                {
                    "lat": lat,
                    "lng": lng,
                    "heading": data.get("heading", 0.0),
                    "speed": data.get("speed", 0.0),
                    "trip_id": ref,
                    "updated_at": "now",
                }
            )
            await self._redis.set(f"loc:trip:{kind}:{ref}", blob, ex=LOC_TTL_S)
            if provider_user_id:
                await self._redis.set(f"loc:{provider_user_id}", blob, ex=LOC_TTL_S)
        except Exception as e:
            logger.debug("Redis bridge failed for %s: %s", ref, e)

    # ── geofence ─────────────────────────────────────────────────────────────
    async def _maybe_check_geofence(self, ref: str, lat: float, lng: float) -> None:
        now = time.monotonic()
        if now - self._geofence_cooldown.get(ref, 0.0) < GEOFENCE_CHECK_EVERY_S:
            return
        self._geofence_cooldown[ref] = now
        try:
            await asyncio.to_thread(self._evaluate_geofence_sync, ref, lat, lng)
        except Exception as e:
            logger.warning("Geofence check failed for %s: %s", ref, e)

    # ── synchronous DB helpers (run via to_thread) ───────────────────────────
    def _resolve_owner(self, ref: str):
        """Return (kind, customer_user_id, provider_user_id) for a booking ref."""
        with Session(engine) as session:
            tow = get_by_reference(session, TowTrip, ref)
            if tow and tow.tow_truck_driver_id:
                drv = session.get(TowTruckDriver, tow.tow_truck_driver_id)
                return "tow", tow.user_id, (drv.user_id if drv else None)
            mech = get_by_reference(session, MechanicTrip, ref)
            if mech and mech.mechanic_id:
                m = session.get(Mechanic, mech.mechanic_id)
                return "mechanic", mech.user_id, (m.user_id if m else None)
        return None, None, None

    def _evaluate_geofence_sync(self, ref: str, lat: float, lng: float) -> None:
        with Session(engine) as session:
            radius = geo.get_config_float(
                session, geo.GEOFENCE_RADIUS_M_KEY, geo.DEFAULT_GEOFENCE_RADIUS_M
            )
            tow = get_by_reference(session, TowTrip, ref)
            if tow:
                self._handle_tow_geofence(session, tow, lat, lng, radius)
                return
            mech = get_by_reference(session, MechanicTrip, ref)
            if mech:
                self._handle_mechanic_geofence(session, mech, lat, lng, radius)

    def _handle_tow_geofence(self, session, tow, lat, lng, radius) -> None:
        if tow.status == "accepted":
            if tow.start_lat is None or tow.start_lng is None:
                return
            if geo.haversine_m(lat, lng, tow.start_lat, tow.start_lng) <= radius:
                tow.status = "arrived"
                session.add(tow)
                session.commit()
                code = booking_otp_service.generate(session, "tow", tow.id)
                self._notify(
                    session,
                    [tow.user_id],
                    "Driver Arrived 🚛",
                    f"Your tow driver is here. Share OTP {code} to start the tow.",
                    {"trip_id": tow.reference_id, "otp": code, "screen": "otp"},
                )
        elif tow.status == "in_progress":
            if tow.end_lat is None or tow.end_lng is None:
                return
            if geo.haversine_m(lat, lng, tow.end_lat, tow.end_lng) <= radius:
                tow.status = "near_destination"
                tow.payment_due_at = now_ist()
                session.add(tow)
                session.commit()
                self._notify(
                    session,
                    [tow.user_id],
                    "Almost There 📍",
                    "Your vehicle is reaching the destination. Please be ready to pay.",
                    {"trip_id": tow.reference_id, "screen": "payment"},
                )

    def _handle_mechanic_geofence(self, session, mech, lat, lng, radius) -> None:
        if mech.status != "accepted":
            return
        if mech.start_lat is None or mech.start_lng is None:
            return
        if geo.haversine_m(lat, lng, mech.start_lat, mech.start_lng) <= radius:
            mech.status = "arrived"
            session.add(mech)
            session.commit()
            code = booking_otp_service.generate(session, "mechanic", mech.id)
            self._notify(
                session,
                [mech.user_id],
                "Mechanic Arrived 🛠️",
                f"Your mechanic is here. Share OTP {code} to confirm the service.",
                {"trip_id": mech.reference_id, "otp": code, "screen": "otp"},
            )

    def _notify(self, session, user_ids, title, body, data) -> None:
        try:
            send_push_notification(
                session=session, user_ids=user_ids, title=title, body=body, data=data
            )
        except Exception as e:
            logger.debug("Push notification failed: %s", e)

    # ── batch upsert ─────────────────────────────────────────────────────────
    async def _flush_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        if not self._buffer:
            return
        snapshot = self._buffer
        self._buffer = {}
        try:
            await asyncio.to_thread(self._flush_sync, snapshot)
        except Exception as e:
            logger.warning("Telemetry batch upsert failed: %s", e)

    def _flush_sync(self, snapshot: Dict[str, Tuple[float, float]]) -> None:
        stamp = now_ist()
        with Session(engine) as session:
            for ref, (lat, lng) in snapshot.items():
                tow = get_by_reference(session, TowTrip, ref)
                if tow and tow.tow_truck_driver_id:
                    drv = session.get(TowTruckDriver, tow.tow_truck_driver_id)
                    if drv:
                        drv.current_lat = lat
                        drv.current_lng = lng
                        drv.location_updated_at = stamp
                        session.add(drv)
                    continue
                mech = get_by_reference(session, MechanicTrip, ref)
                if mech and mech.mechanic_id:
                    m = session.get(Mechanic, mech.mechanic_id)
                    if m:
                        m.current_lat = lat
                        m.current_lng = lng
                        m.location_updated_at = stamp
                        session.add(m)
            session.commit()
