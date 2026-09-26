"""Load test for NDTP ingest: N simulated terminals against a real ``NdtpServer``.

The server runs in its own process with a minimal consumer (records what arrives), an event-loop
lag sampler and CPU sampling. Terminals are spread over several client processes so the load
generator isn't the bottleneck. Every packet is timestamped just before ``write()`` and matched
with its arrival at the consumer by ``(unit_id, request_id)``.

Phases:
1. connect storm — all units dial at the same instant, then send a handshake;
2. traffic — ``--packets`` realtime packets per unit every ``--interval`` seconds. ``lockstep``:
   every unit sends on the same tick (worst case, "all at once"); ``spread``: send times are
   spread evenly over the interval (closer to reality).

Measures only the ingest layer (socket → decode → queue → consumer), not what the consumer does
with fixes later.

Usage, from the repo root::

    python backend/scripts/ndtp_loadtest.py --units 4000 --interval 1 --packets 20 --mode lockstep
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import os
import pickle
import resource
import socket
import sys
import tempfile
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ndtp import NdtpServer  # noqa: E402
from app.ndtp.protocol import encode_handshake, encode_nav_cell, encode_realtime  # noqa: E402

FIRST_UNIT_ID = 500_000


def raise_fd_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
    if soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError):
            pass


def use_loop(kind: str) -> None:
    if kind == "uvloop":
        import uvloop
        uvloop.install()


# ----------------------------------------------------------------------------- server process


def server_proc(port: int, args: argparse.Namespace, ready, done, out: str) -> None:
    raise_fd_limit()
    use_loop(args.loop)

    async def run() -> None:
        q: asyncio.Queue = asyncio.Queue(maxsize=args.queue)
        srv = NdtpServer(q, host="127.0.0.1", port=port, idle_timeout=600,
                         max_connections=args.max_connections, backlog=args.backlog)
        await srv.start()
        units, reqs, t_read, t_recv = array("q"), array("q"), array("d"), array("d")
        lags, cpu = array("d"), []

        async def consume() -> None:
            while True:
                fix = await q.get()
                units.append(fix.unit_id)
                reqs.append(fix.request_id)
                t_read.append(fix.received_at)  # when the server read the bytes off the socket
                t_recv.append(time.time())      # when the consumer got the fix off the queue

        async def sample() -> None:
            while True:
                t = time.perf_counter()
                await asyncio.sleep(0.05)
                lags.append(time.perf_counter() - t - 0.05)
                cpu.append((time.time(), time.process_time(), q.qsize()))

        tasks = [asyncio.create_task(consume()), asyncio.create_task(sample())]
        ready.set()
        await asyncio.get_running_loop().run_in_executor(None, done.wait)
        snap = srv.snapshot()
        with open(out, "wb") as f:
            pickle.dump({"units": units, "reqs": reqs, "t_read": t_read, "t_recv": t_recv, "lags": lags, "cpu": cpu,
                         "stats": snap["stats"], "framing": snap["framing"],
                         "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}, f)
        for t in tasks:
            t.cancel()
        await srv.stop()

    asyncio.run(run())


# ----------------------------------------------------------------------------- client processes


def client_proc(port: int, unit_ids: list[int], args: argparse.Namespace, t_connect: float, t_send: float,
                out: str) -> None:
    raise_fd_limit()
    use_loop(args.loop)

    async def run() -> dict:
        s_units, s_reqs, s_times = array("q"), array("q"), array("d")
        connect_s, failures = array("d"), {}

        async def terminal(unit: int, phase: float) -> None:
            await asyncio.sleep(max(0.0, t_connect - time.time()))
            t = time.perf_counter()
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), args.connect_timeout)
            except (OSError, TimeoutError) as e:
                failures[type(e).__name__] = failures.get(type(e).__name__, 0) + 1
                return
            connect_s.append(time.perf_counter() - t)
            w.write(encode_handshake(unit))
            nav = encode_nav_cell(timestamp=int(time.time()), latitude=55.75, longitude=37.61, speed_kmh=30)
            try:
                for k in range(args.packets):
                    await asyncio.sleep(max(0.0, t_send + k * args.interval + phase - time.time()))
                    frame = encode_realtime(unit, nav, request_id=k + 2)
                    s_units.append(unit)
                    s_reqs.append(k + 2)
                    s_times.append(time.time())
                    w.write(frame)
                    await w.drain()
                await asyncio.sleep(1.0)
            except OSError as e:
                failures["send_" + type(e).__name__] = failures.get("send_" + type(e).__name__, 0) + 1
            finally:
                w.close()

        n_all = args.units
        await asyncio.gather(*(
            terminal(u, 0.0 if args.mode == "lockstep" else (u - FIRST_UNIT_ID) / n_all * args.interval)
            for u in unit_ids))
        return {"units": s_units, "reqs": s_reqs, "times": s_times, "connect_s": connect_s, "failures": failures}

    res = asyncio.run(run())
    with open(out, "wb") as f:
        pickle.dump(res, f)


# ----------------------------------------------------------------------------- orchestration + report


def pct(values, p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--units", type=int, default=1000)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between packets of one unit")
    ap.add_argument("--packets", type=int, default=20, help="realtime packets per unit")
    ap.add_argument("--mode", choices=["lockstep", "spread"], default="lockstep")
    ap.add_argument("--loop", choices=["asyncio", "uvloop"], default="asyncio")
    ap.add_argument("--clients", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--backlog", type=int, default=4096)
    ap.add_argument("--max-connections", type=int, default=100_000)
    ap.add_argument("--queue", type=int, default=100_000, help="sink queue size")
    ap.add_argument("--connect-timeout", type=float, default=30.0)
    ap.add_argument("--settle", type=float, default=0.0,
                    help="seconds between connect storm and first packet (default: 3 + units/1000)")
    args = ap.parse_args()
    raise_fd_limit()

    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="ndtp-load-"))
    ctx = mp.get_context("spawn")
    ready, done = ctx.Event(), ctx.Event()
    srv = ctx.Process(target=server_proc, args=(port, args, ready, done, str(tmp / "server.pkl")))
    srv.start()
    if not ready.wait(30):
        sys.exit("server did not start")

    ids = list(range(FIRST_UNIT_ID, FIRST_UNIT_ID + args.units))
    settle = args.settle or 3.0 + args.units / 1000
    t_connect = time.time() + 2.0 + args.clients * 0.3
    t_send = t_connect + settle
    clients = [ctx.Process(target=client_proc,
                           args=(port, ids[i::args.clients], args, t_connect, t_send, str(tmp / f"client{i}.pkl")))
               for i in range(args.clients)]
    for c in clients:
        c.start()
    for c in clients:
        c.join()
    time.sleep(1.0)
    done.set()
    srv.join(30)

    S = pickle.loads((tmp / "server.pkl").read_bytes())
    C = [pickle.loads((tmp / f"client{i}.pkl").read_bytes()) for i in range(args.clients)]

    sent = {}
    for c in C:
        for u, r, t in zip(c["units"], c["reqs"], c["times"]):
            sent[(u, r)] = t
    recv, read = {}, {}
    for u, r, t_rd, t in zip(S["units"], S["reqs"], S["t_read"], S["t_recv"]):
        recv[(u, r)] = t
        read[(u, r)] = t_rd
    lat_ms = [(recv[k] - t) * 1000 for k, t in sent.items() if k in recv]
    sock_ms = [(read[k] - t) * 1000 for k, t in sent.items() if k in read]         # write() -> server read
    queue_ms = [(recv[k] - read[k]) * 1000 for k in recv]                           # server read -> consumer
    send_times = sorted(sent.values())
    achieved = len(send_times) / (send_times[-1] - send_times[0]) if len(send_times) > 1 else float("nan")
    connect_ms = [x * 1000 for c in C for x in c["connect_s"]]
    failures: dict[str, int] = {}
    for c in C:
        for k, v in c["failures"].items():
            failures[k] = failures.get(k, 0) + v

    # per-tick burst drain (lockstep): tick start -> last packet of that tick at the consumer
    # and send spread: tick start -> last packet of that tick written by the generator. If the spread is
    # close to the drain, the generator (not the server) set the pace and the "burst" was really a ramp.
    drain_ms, spread_ms = [], []
    if args.mode == "lockstep":
        last_recv: dict[int, float] = {}
        last_send: dict[int, float] = {}
        for (u, r), t in recv.items():
            last_recv[r] = max(last_recv.get(r, 0.0), t)
        for (u, r), t in sent.items():
            last_send[r] = max(last_send.get(r, 0.0), t)
        tick = lambda r: t_send + (r - 2) * args.interval  # noqa: E731
        drain_ms = [(t - tick(r)) * 1000 for r, t in last_recv.items()]
        spread_ms = [(t - tick(r)) * 1000 for r, t in last_send.items()]

    window = (t_send, t_send + args.packets * args.interval)
    cpu = [c for c in S["cpu"] if window[0] <= c[0] <= window[1]]
    cpu_pct = ((cpu[-1][1] - cpu[0][1]) / (cpu[-1][0] - cpu[0][0]) * 100) if len(cpu) > 1 else float("nan")
    max_q = max((c[2] for c in cpu), default=0)
    lag_ms = [x * 1000 for x in S["lags"]]
    rate = args.units / args.interval

    print(f"\n=== NDTP load test: {args.units} units, every {args.interval:g}s, {args.mode}, {args.loop}, "
          f"backlog {args.backlog}, {args.clients} client procs ===")
    print(f"offered load        {rate:,.0f} packets/s nominal, {achieved:,.0f} packets/s achieved by the generator "
          f"({args.units * args.packets:,} packets total)")
    print(f"connect storm       {len(connect_ms)}/{args.units} connected, failures {failures or 'none'}")
    print(f"  connect time      p50 {pct(connect_ms, 50):.0f} ms  p99 {pct(connect_ms, 99):.0f} ms  "
          f"max {max(connect_ms, default=float('nan')):.0f} ms")
    print(f"delivered           {len(lat_ms):,}/{len(sent):,} sent packets reached the consumer "
          f"(lost {len(sent) - len(lat_ms)}), server dropped {S['stats']['fixes_dropped']}")
    print(f"latency send→queue  p50 {pct(lat_ms, 50):.1f} ms  p95 {pct(lat_ms, 95):.1f} ms  "
          f"p99 {pct(lat_ms, 99):.1f} ms  max {max(lat_ms, default=float('nan')):.1f} ms")
    print(f"  = socket→read     p50 {pct(sock_ms, 50):.1f} ms  p99 {pct(sock_ms, 99):.1f} ms  "
          f"(kernel buffers + waiting for the event loop to read)")
    print(f"  + read→consumer   p50 {pct(queue_ms, 50):.1f} ms  p99 {pct(queue_ms, 99):.1f} ms  "
          f"(in-process queue: consumer scheduled after the readers)")
    if drain_ms:
        print(f"send spread         median {pct(spread_ms, 50):.0f} ms  max {max(spread_ms):.0f} ms  "
              f"(tick → last packet of the tick written by the generator)")
        print(f"burst drain         median {pct(drain_ms, 50):.0f} ms  max {max(drain_ms):.0f} ms  "
              f"(tick → last packet of the tick consumed; must stay < interval {args.interval * 1000:.0f} ms)")
    rss_mb = S["max_rss_kb"] / (1024 * 1024 if sys.platform == "darwin" else 1024)  # macOS reports bytes
    print(f"server CPU          {cpu_pct:.0f}% of one core during traffic; peak queue depth {max_q}")
    print(f"server memory       peak RSS {rss_mb:.0f} MB ({rss_mb * 1024 / args.units:.1f} KB per connection incl. baseline)")
    print(f"event-loop lag      p50 {pct(lag_ms, 50):.1f} ms  p99 {pct(lag_ms, 99):.1f} ms  max {max(lag_ms):.1f} ms")
    print(f"server counters     {S['stats']}")
    print(f"framing             {S['framing']}")


if __name__ == "__main__":
    main()
