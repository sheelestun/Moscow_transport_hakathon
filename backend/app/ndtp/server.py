"""NDTP TCP server: accepts terminal connections and turns their byte streams into navigation fixes.

Each terminal (or emulated unit) opens its own TCP connection, sends a handshake, then realtime
packets. For every realtime packet with a ``G6CellNav00`` the server puts an ``NdtpFix`` on the
sink queue. Everything after that — unit → vehicle mapping, dataset clock, schedule matching —
belongs to the consumer, not here.

Robustness rules (criterion 5: no crash on link loss, recover after reconnect):

* a slow consumer never blocks socket reads: fixes go in with ``put_nowait``, overflow is dropped
  and counted in ``stats.fixes_dropped``;
* a new handshake for a unit closes that unit's older connection (newest wins — a terminal that
  reconnected after a network loss leaves a half-open socket behind);
* connections silent for ``idle_timeout`` seconds are closed;
* framing and decode errors are counted, never fatal to the connection.

The server sends nothing back: the spec defines no acknowledgement packet, and the emulator
ignores whatever the server writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from dataclasses import asdict, dataclass

from .protocol import (
    DecoderStats,
    FrameDecoder,
    Handshake,
    NavCell,
    NdtpDecodeError,
    Realtime,
    UnknownMessage,
    decode_frame,
)

log = logging.getLogger(__name__)

READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class NdtpFix:
    """A navigation fix as received: who sent it, what it says, when it arrived."""

    unit_id: int
    nav: NavCell
    received_at: float  # server wall clock, Unix seconds
    conn_id: int
    request_id: int     # NPH request counter of the packet: +1 per packet, gaps = lost packets


@dataclass(slots=True)
class ServerStats:
    connections_total: int = 0
    connections_rejected: int = 0
    connections_superseded: int = 0  # closed because the same unit reconnected
    connections_idle_closed: int = 0
    handshakes: int = 0
    fixes: int = 0
    fixes_dropped: int = 0           # sink queue full
    frames_without_nav: int = 0
    frames_before_handshake: int = 0
    unknown_messages: int = 0
    decode_errors: int = 0


@dataclass(slots=True)
class _Connection:
    conn_id: int
    peer: str
    writer: asyncio.StreamWriter
    decoder: FrameDecoder
    connected_at: float
    last_seen: float
    unit_id: int | None = None
    fixes: int = 0

    def snapshot(self) -> dict:
        return {
            "conn_id": self.conn_id,
            "unit_id": self.unit_id,
            "peer": self.peer,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "fixes": self.fixes,
            "framing": asdict(self.decoder.stats),
        }


class NdtpServer:
    """Asyncio TCP server for NDTP terminals. ``start()`` binds, ``stop()`` closes everything."""

    def __init__(
        self,
        sink: asyncio.Queue[NdtpFix],
        *,
        host: str = "0.0.0.0",
        port: int = 9201,
        idle_timeout: float = 300.0,
        max_connections: int = 20_000,  # headroom over a city-scale fleet; see scripts/ndtp_loadtest.py
        backlog: int = 4096,  # a fleet reconnecting at once (e.g. after a restart) overflows asyncio's default
                              # 100: dropped SYNs, multi-second connects. Linux clamps it to net.core.somaxconn.
    ) -> None:
        self.sink = sink
        self.host = host
        self.port = port
        self.idle_timeout = idle_timeout
        self.max_connections = max_connections
        self.backlog = backlog
        self.stats = ServerStats()
        self._server: asyncio.Server | None = None
        self._conns: dict[int, _Connection] = {}
        self._by_unit: dict[int, _Connection] = {}
        self._closed_framing = DecoderStats()  # framing stats of connections already gone
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, backlog=self.backlog)
        self.port = self._server.sockets[0].getsockname()[1]  # resolves port=0 in tests
        log.info("NDTP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for conn in list(self._conns.values()):
            conn.writer.close()
        await self._server.wait_closed()
        self._server = None
        log.info("NDTP server stopped")

    # ------------------------------------------------------------------ introspection

    def snapshot(self) -> dict:
        """State for ``/health`` and metrics."""
        framing = DecoderStats(**asdict(self._closed_framing))
        for conn in self._conns.values():
            for name, value in asdict(conn.decoder.stats).items():
                setattr(framing, name, getattr(framing, name) + value)
        return {
            "listening": self._server is not None,
            "port": self.port,
            "connections": [c.snapshot() for c in self._conns.values()],
            "units_connected": sorted(self._by_unit),
            "sink_queue_depth": self.sink.qsize(),
            "stats": asdict(self.stats),
            "framing": asdict(framing),
        }

    # ------------------------------------------------------------------ per-connection

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = _format_peer(writer.get_extra_info("peername"))
        if len(self._conns) >= self.max_connections:
            self.stats.connections_rejected += 1
            log.warning("NDTP connection from %s rejected: %d connections open", peer, len(self._conns))
            writer.close()
            return

        now = time.time()
        conn = _Connection(next(self._ids), peer, writer, FrameDecoder(), connected_at=now, last_seen=now)
        self._conns[conn.conn_id] = conn
        self.stats.connections_total += 1
        log.info("NDTP conn %d opened from %s", conn.conn_id, peer)
        reason = "eof"
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=self.idle_timeout)
                except TimeoutError:
                    self.stats.connections_idle_closed += 1
                    reason = "idle timeout"
                    break
                if not data:
                    break
                conn.last_seen = time.time()
                for frame in conn.decoder.feed(data):
                    try:
                        self._dispatch(conn, frame.npl.peer_address, decode_frame(frame))
                    except NdtpDecodeError as e:
                        self.stats.decode_errors += 1
                        log.warning("NDTP conn %d: %s", conn.conn_id, e)
        except (ConnectionError, OSError) as e:
            reason = type(e).__name__
        finally:
            self._drop(conn)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            log.info("NDTP conn %d closed (unit %s, %s, %d fixes)", conn.conn_id, conn.unit_id, reason, conn.fixes)

    def _dispatch(self, conn: _Connection, npl_peer: int, msg) -> None:
        if isinstance(msg, Handshake):
            self.stats.handshakes += 1
            if msg.peer_address != npl_peer:
                log.warning("NDTP conn %d: handshake peer %d != NPL peer %d", conn.conn_id, msg.peer_address, npl_peer)
            self._bind(conn, msg.peer_address)
            return

        if isinstance(msg, Realtime):
            if conn.unit_id is None:  # tolerate terminals that skip the handshake: NPL carries the id too
                self.stats.frames_before_handshake += 1
                self._bind(conn, npl_peer)
            if msg.nav is None:
                self.stats.frames_without_nav += 1
                return
            fix = NdtpFix(unit_id=conn.unit_id, nav=msg.nav, received_at=conn.last_seen, conn_id=conn.conn_id,
                          request_id=msg.nph.request_id)
            try:
                self.sink.put_nowait(fix)
            except asyncio.QueueFull:
                self.stats.fixes_dropped += 1
                return
            conn.fixes += 1
            self.stats.fixes += 1
            return

        if isinstance(msg, UnknownMessage):
            self.stats.unknown_messages += 1

    def _bind(self, conn: _Connection, unit_id: int) -> None:
        old = self._by_unit.get(unit_id)
        if old is not None and old is not conn:
            self.stats.connections_superseded += 1
            log.info("NDTP unit %d reconnected: closing older conn %d", unit_id, old.conn_id)
            old.writer.close()  # its read loop sees EOF and cleans up
        conn.unit_id = unit_id
        self._by_unit[unit_id] = conn
        log.info("NDTP conn %d bound to unit %d", conn.conn_id, unit_id)

    def _drop(self, conn: _Connection) -> None:
        self._conns.pop(conn.conn_id, None)
        if conn.unit_id is not None and self._by_unit.get(conn.unit_id) is conn:
            del self._by_unit[conn.unit_id]
        for name, value in asdict(conn.decoder.stats).items():
            setattr(self._closed_framing, name, getattr(self._closed_framing, name) + value)


def _format_peer(peername) -> str:
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return str(peername)
