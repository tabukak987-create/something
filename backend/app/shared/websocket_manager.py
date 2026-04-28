"""
WebSocket Connection Manager
Manages broadcast groups so any backend service can push real-time
price / risk updates to all connected browser clients.

Usage
-----
From any FastAPI route:
    from app.shared.websocket_manager import ws_manager
    await ws_manager.broadcast("prices", {"isin": "IN0020240013", "ytm": 7.12})

From a WebSocket endpoint:
    @router.websocket("/ws/{topic}")
    async def ws_endpoint(ws: WebSocket, topic: str):
        await ws_manager.connect(topic, ws)
        try:
            while True:
                await ws.receive_text()   # keep alive / handle pings
        except WebSocketDisconnect:
            ws_manager.disconnect(topic, ws)
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from .logger import get_logger

log = get_logger("ws_manager")


class WebSocketManager:
    """Thread-safe broadcast hub organised by topic."""

    def __init__(self) -> None:
        # topic → set of live WebSocket connections
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    # ―― Lifecycle ―――――――――――――――――――――――――――――――――――――――――――――――――――

    async def connect(self, topic: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[topic].add(ws)
        total = sum(len(v) for v in self._connections.values())
        log.info("ws_connect", topic=topic, total_connections=total)

    def disconnect(self, topic: str, ws: WebSocket) -> None:
        self._connections[topic].discard(ws)
        if not self._connections[topic]:
            del self._connections[topic]
        log.debug("ws_disconnect", topic=topic)

    # ―― Broadcast ―――――――――――――――――――――――――――――――――――――――――――――――――――

    async def broadcast(self, topic: str, payload: Any) -> int:
        """
        Send *payload* (dict or str) to every client subscribed to *topic*.
        Returns the number of successful sends.
        """
        sockets = list(self._connections.get(topic, set()))
        if not sockets:
            return 0
        message = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        sends = await asyncio.gather(
            *[self._safe_send(ws, message) for ws in sockets],
            return_exceptions=True,
        )
        ok = sum(1 for r in sends if r is True)
        failed = [s for r, s in zip(sends, sockets) if r is not True]
        for ws in failed:
            self.disconnect(topic, ws)
        if failed:
            log.warning("ws_broadcast_partial", topic=topic, ok=ok, failed=len(failed))
        return ok

    async def broadcast_all(self, payload: Any) -> int:
        """Broadcast to every connected client across all topics."""
        total = 0
        for topic in list(self._connections):
            total += await self.broadcast(topic, payload)
        return total

    async def _safe_send(self, ws: WebSocket, message: str) -> bool:
        try:
            await ws.send_text(message)
            return True
        except Exception as exc:
            log.debug("ws_send_error", error=str(exc))
            return False

    # ―― Introspection ――――――――――――――――――――――――――――――――――――――――――――――

    @property
    def topics(self) -> list[str]:
        return list(self._connections)

    def connection_count(self, topic: str | None = None) -> int:
        if topic:
            return len(self._connections.get(topic, set()))
        return sum(len(v) for v in self._connections.values())


# Singleton — import this everywhere
ws_manager = WebSocketManager()
