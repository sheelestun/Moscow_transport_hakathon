"""Для остановок без ``building_address`` в датасете ищем название в OSM Overpass API.

У ~16% уникальных координат (135 из 847) `building_address` пустой — на карточке
показываем «остановка без адреса», что выглядит плохо. OSM обычно знает эти места
(тег ``highway=bus_stop`` или ``public_transport=platform`` с ``name``).

Один запуск, коммитим ``backend/app/state/stop_names.json`` в git. Пере-собирать
нужно только если поменялся ``schedule_plan.csv``.

Запуск:
    python infra/build_stop_names.py --dataset ./dataset/validate/schedule_plan.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

_POINT = re.compile(r"POINT \(([-\d.]+) ([-\d.]+)\)")
OVERPASS = "https://overpass-api.de/api/interpreter"
RADIUS_M = 120  # у Ново-Переделкино и в промзонах остановка может быть >50м от точки CSV

log = logging.getLogger("build_stop_names")


def collect_empty(schedule_path: Path) -> list[tuple[float, float]]:
    """Уникальные (lon, lat) с пустым building_address."""
    seen: dict[tuple[float, float], str] = {}
    with schedule_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m = _POINT.match(r["geom"])
            if not m:
                continue
            lon, lat = round(float(m.group(1)), 5), round(float(m.group(2)), 5)
            addr = (r.get("building_address") or "").strip()
            if (lon, lat) not in seen or (not seen[(lon, lat)] and addr):
                seen[(lon, lat)] = addr
    return [c for c, a in seen.items() if not a]


def query_overpass(lon: float, lat: float, radius_m: int = RADIUS_M) -> str | None:
    """Ближайший ``bus_stop``/``platform`` с ``name`` внутри radius_m метров."""
    q = (
        f"[out:json][timeout:15];"
        f'(node(around:{radius_m},{lat},{lon})["highway"="bus_stop"]["name"];'
        f'node(around:{radius_m},{lat},{lon})["public_transport"="platform"]["name"];'
        f'node(around:{radius_m},{lat},{lon})["public_transport"="stop_position"]["name"];);'
        f"out tags center;"
    )
    data = urllib.parse.urlencode({"data": q}).encode("utf-8")
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": "msktrans-stops/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    els = body.get("elements", [])
    if not els:
        return None
    for e in els:  # первое имя, всё равно все рядом (в пределах радиуса)
        n = e.get("tags", {}).get("name")
        if n:
            return n
    return None


def build(schedule_path: Path, sleep_s: float) -> dict[str, str]:
    todo = collect_empty(schedule_path)
    log.info("остановок без адреса: %d", len(todo))
    out: dict[str, str] = {}
    for i, (lon, lat) in enumerate(todo, 1):
        try:
            name = query_overpass(lon, lat)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log.warning("[%d/%d] %.5f,%.5f: %s", i, len(todo), lat, lon, e)
            name = None
        if name:
            out[f"{lat:.5f},{lon:.5f}"] = name
            log.info("[%d/%d] %.5f,%.5f: %s", i, len(todo), lat, lon, name)
        else:
            log.info("[%d/%d] %.5f,%.5f: —", i, len(todo), lat, lon)
        time.sleep(sleep_s)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="dataset/validate/schedule_plan.csv", type=Path)
    parser.add_argument("--out", default="backend/app/state/stop_names.json", type=Path)
    parser.add_argument("--sleep", type=float, default=0.6)  # ~1.5 rps, вежливо к бесплатной Overpass
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    names = build(args.dataset, args.sleep)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    log.info("готово: %d названий из %d пустых → %s", len(names), len(collect_empty(args.dataset)), args.out)


if __name__ == "__main__":
    main()
