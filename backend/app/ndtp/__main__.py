"""Standalone NDTP listener for debugging ingest without the rest of the backend.

    python -m app.ndtp                     # :9201, log every fix + a summary every 10 s
    python -m app.ndtp --quiet --every 5   # summaries only (use under load)

Run from ``backend/``. ``age`` in the fix log is server receive time minus the terminal's timestamp:
network delay plus clock skew between terminal and server, at 1-second resolution.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from .server import NdtpServer

log = logging.getLogger("ndtp")


async def run(args: argparse.Namespace) -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=args.queue)
    srv = NdtpServer(q, host=args.host, port=args.port, idle_timeout=args.idle_timeout)
    await srv.start()

    async def consume() -> None:
        while True:
            fix = await q.get()
            if args.quiet:
                continue
            nav = fix.nav
            log.info("fix unit=%d req=%d t=%s lat=%.6f lon=%.6f valid=%s speed=%d course=%d age=%.1fs",
                     fix.unit_id, fix.request_id, datetime.fromtimestamp(nav.timestamp, UTC).strftime("%H:%M:%SZ"),
                     nav.latitude, nav.longitude, nav.location_valid, nav.speed_avg_kmh, nav.course_deg,
                     fix.received_at - nav.timestamp)

    async def summarize() -> None:
        prev = 0
        while True:
            await asyncio.sleep(args.every)
            snap = srv.snapshot()
            s, f = snap["stats"], snap["framing"]
            log.info("summary: %d conns, units %s, %.1f fixes/s, total %d, dropped %d, queue %d, crc_err %d, "
                     "discarded %d B",
                     len(snap["connections"]), _short(snap["units_connected"]), (s["fixes"] - prev) / args.every,
                     s["fixes"], s["fixes_dropped"], snap["sink_queue_depth"], f["crc_errors"], f["bytes_discarded"])
            prev = s["fixes"]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(consume()), asyncio.create_task(summarize())]
    await stop.wait()
    for t in tasks:
        t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*tasks)
    await srv.stop()


def _short(units: list[int], limit: int = 8) -> str:
    return str(units) if len(units) <= limit else f"{units[:limit]} +{len(units) - limit} more"


def main() -> None:
    ap = argparse.ArgumentParser(description="Standalone NDTP listener")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9201)
    ap.add_argument("--idle-timeout", type=float, default=300.0)
    ap.add_argument("--queue", type=int, default=100_000)
    ap.add_argument("--every", type=float, default=10.0, help="seconds between summary lines")
    ap.add_argument("--quiet", action="store_true", help="don't log individual fixes")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
