"""WebSocket push to dashboards: ``vehicle.update`` every tick, alert and what-if events as they happen.

Events come from sync callbacks (alert engine, what-if) through ``Hub.publish``; one task sends them and
the periodic vehicle snapshot, computed once per tick for all clients. A client that can't take a message
within ``send_timeout_s`` is dropped instead of slowing everyone else down; the dashboard reconnects on
its own (``frontend/js/api.js``) and gets a fresh snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .alerts import alert_payload
from .dashboard import vehicles_now

log = logging.getLogger(__name__)

router = APIRouter()


class Hub:
    def __init__(self, *, send_timeout_s: float = 2.0) -> None:
        self.clients: set[WebSocket] = set()
        self.send_timeout_s = send_timeout_s
        self.dropped_clients = 0
        self.events_dropped = 0
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=10_000)

    def publish(self, msg: dict) -> None:
        try:
            self._events.put_nowait(msg)
        except asyncio.QueueFull:
            self.events_dropped += 1

    async def send_all(self, msg: dict) -> None:
        text = json.dumps(msg, ensure_ascii=False, default=str)
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(text), self.send_timeout_s)
            except Exception:  # noqa: BLE001 — gone or too slow: drop it, it will reconnect
                self.clients.discard(ws)
                self.dropped_clients += 1

    async def run(self, snapshot: Callable[[], dict], tick_s: float = 1.0) -> None:
        next_tick = time.monotonic()
        while True:
            try:
                msg = await asyncio.wait_for(self._events.get(), max(0.0, next_tick - time.monotonic()))
                await self.send_all(msg)
            except TimeoutError:
                if self.clients:
                    await self.send_all(snapshot())
                next_tick = time.monotonic() + tick_s


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """Dashboard stream: a snapshot on connect, then ``vehicle.update`` / ``alert.*`` / ``whatif.result``."""
    state = websocket.app.state
    await websocket.accept()
    try:
        await websocket.send_text(json.dumps({"type": "vehicle.update", "vehicles": vehicles_now(state)},
                                             ensure_ascii=False, default=str))
        for a in list(state.alerts.active.values()):
            await websocket.send_text(json.dumps(alert_payload(state.clock, a, "alert.new"), ensure_ascii=False,
                                                 default=str))
        state.hub.clients.add(websocket)
        while True:
            await websocket.receive_text()  # the dashboard sends nothing; this just waits for the disconnect
    except WebSocketDisconnect:
        pass
    finally:
        state.hub.clients.discard(websocket)
