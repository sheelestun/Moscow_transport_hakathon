"""NDTP TCP server tests over real sockets on 127.0.0.1 (ephemeral port)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.ndtp.protocol import encode_handshake, encode_nav_cell, encode_realtime
from app.ndtp.server import NdtpServer

FIXTURES = Path(__file__).parent / "fixtures"


async def serve(maxsize: int = 1000, **kw) -> tuple[NdtpServer, asyncio.Queue]:
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    srv = NdtpServer(q, host="127.0.0.1", port=0, **kw)
    await srv.start()
    return srv, q


async def connect(srv: NdtpServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", srv.port)


async def until(cond, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


def realtime(unit_id: int, request_id: int) -> bytes:
    nav = encode_nav_cell(timestamp=1_767_670_500 + request_id, latitude=55.75, longitude=37.61)
    return encode_realtime(unit_id, nav, request_id=request_id)


def test_real_emulator_stream_in_small_chunks() -> None:
    async def run() -> None:
        srv, q = await serve()
        _, w = await connect(srv)
        raw = (FIXTURES / "emulator_explicit_moscow.bin").read_bytes()
        for i in range(0, len(raw), 7):
            w.write(raw[i : i + 7])
            await w.drain()
        await until(lambda: q.qsize() == 5)
        fixes = [q.get_nowait() for _ in range(5)]
        assert {f.unit_id for f in fixes} == {664030}
        assert fixes[0].nav.latitude == 55.7551234
        snap = srv.snapshot()
        assert snap["units_connected"] == [664030]
        assert snap["stats"]["handshakes"] == 1 and snap["stats"]["fixes"] == 5
        w.close()
        await until(lambda: not srv.snapshot()["connections"])
        assert srv.snapshot()["units_connected"] == []
        await srv.stop()

    asyncio.run(run())


def test_reconnect_of_same_unit_supersedes_older_connection() -> None:
    async def run() -> None:
        srv, q = await serve()
        r_old, w_old = await connect(srv)
        w_old.write(encode_handshake(42))
        await until(lambda: srv.snapshot()["units_connected"] == [42])
        _, w_new = await connect(srv)
        w_new.write(encode_handshake(42) + realtime(42, 2))
        await until(lambda: srv.stats.connections_superseded == 1)
        assert await asyncio.wait_for(r_old.read(), 1.0) == b""  # server closed the old socket
        await until(lambda: len(srv.snapshot()["connections"]) == 1)
        assert srv.snapshot()["units_connected"] == [42]
        assert (await q.get()).unit_id == 42
        w_new.close()
        await srv.stop()

    asyncio.run(run())


def test_idle_connection_is_closed() -> None:
    async def run() -> None:
        srv, _ = await serve(idle_timeout=0.2)
        r, w = await connect(srv)
        w.write(encode_handshake(7))
        assert await asyncio.wait_for(r.read(), 2.0) == b""
        await until(lambda: not srv.snapshot()["connections"])
        assert srv.stats.connections_idle_closed == 1
        await srv.stop()

    asyncio.run(run())


def test_full_sink_drops_fixes_but_keeps_connection() -> None:
    async def run() -> None:
        srv, q = await serve(maxsize=2)
        _, w = await connect(srv)
        w.write((FIXTURES / "emulator_explicit_moscow.bin").read_bytes())
        await until(lambda: srv.stats.fixes + srv.stats.fixes_dropped == 5)
        assert (srv.stats.fixes, srv.stats.fixes_dropped, q.qsize()) == (2, 3, 2)
        assert srv.snapshot()["units_connected"] == [664030]
        w.close()
        await srv.stop()

    asyncio.run(run())


def test_garbage_and_corrupt_frames_do_not_kill_connection() -> None:
    async def run() -> None:
        srv, q = await serve()
        _, w = await connect(srv)
        corrupt = bytearray(realtime(5, 2))
        corrupt[-1] ^= 0xFF
        w.write(b"\x00garbage" + bytes(corrupt) + encode_handshake(5) + realtime(5, 3))
        fix = await asyncio.wait_for(q.get(), 2.0)
        assert fix.unit_id == 5 and fix.nav.timestamp == 1_767_670_503
        framing = srv.snapshot()["framing"]
        assert framing["crc_errors"] == 1 and framing["bytes_discarded"] > 0
        w.close()
        await srv.stop()

    asyncio.run(run())


def test_realtime_without_handshake_uses_npl_peer_address() -> None:
    async def run() -> None:
        srv, q = await serve()
        _, w = await connect(srv)
        w.write(realtime(99, 2))
        assert (await asyncio.wait_for(q.get(), 2.0)).unit_id == 99
        assert srv.stats.frames_before_handshake == 1
        w.close()
        await srv.stop()

    asyncio.run(run())


def test_connection_limit_and_stop_close_clients() -> None:
    async def run() -> None:
        srv, _ = await serve(max_connections=1)
        r1, w1 = await connect(srv)
        w1.write(encode_handshake(1))
        await until(lambda: srv.snapshot()["units_connected"] == [1])
        r2, _ = await connect(srv)
        assert await asyncio.wait_for(r2.read(), 1.0) == b""
        assert srv.stats.connections_rejected == 1
        await srv.stop()
        assert await asyncio.wait_for(r1.read(), 1.0) == b""
        assert not srv.snapshot()["listening"]

    asyncio.run(run())
