"""Для всех остановок расписания подтягиваем человеческие названия из OSM.

Проблема: в датасете у 16% остановок ``building_address`` пуст, а у остальных
это адрес здания — «ул. Маршала Василевского, д.17», а не «Метро Щукинская».
Диспетчер не опознаёт остановку по адресу.

Решение: одним запросом к Overpass выкачиваем все именованные остановки
(``highway=bus_stop`` или ``public_transport=platform``/``stop_position``)
в bbox Москвы, для каждой остановки датасета берём ближайшую OSM
в пределах ``RADIUS_M``. Результат — ``backend/app/state/stop_names.json``,
``schedule.py`` предпочитает его ``building_address``.

Один запуск, коммитим json в git. Перегенерировать надо только если
поменялся ``schedule_plan.csv``.

Запуск:
    python infra/build_stop_names.py --dataset ./dataset/validate/schedule_plan.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_POINT = re.compile(r"POINT \(([-\d.]+) ([-\d.]+)\)")
OVERPASS = "https://overpass-api.de/api/interpreter"
# BBOX (south, west, north, east) — Москва + область (все датасетные остановки укладываются)
BBOX = (55.4, 37.1, 56.05, 37.95)
RADIUS_M = 80

log = logging.getLogger("build_stop_names")


def collect_stops(schedule_path: Path) -> dict[tuple[float, float], str]:
    """Уникальные (lat, lon) → building_address (пусто если нет)."""
    seen: dict[tuple[float, float], str] = {}
    with schedule_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m = _POINT.match(r["geom"])
            if not m:
                continue
            lat = round(float(m[2]), 5)
            lon = round(float(m[1]), 5)
            addr = (r.get("building_address") or "").strip()
            if (lat, lon) not in seen or (not seen[(lat, lon)] and addr):
                seen[(lat, lon)] = addr
    return seen


def fetch_osm_stops(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, str]]:
    s, w, n, e = bbox
    q = (
        f"[out:json][timeout:120];"
        f'(node["highway"="bus_stop"]["name"]({s},{w},{n},{e});'
        f'node["public_transport"="platform"]["name"]({s},{w},{n},{e});'
        f'node["public_transport"="stop_position"]["name"]({s},{w},{n},{e}););'
        f"out tags center;"
    )
    data = urllib.parse.urlencode({"data": q}).encode("utf-8")
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": "msktrans-stops/2.0"})
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read())
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as ex:
            log.warning("Overpass attempt %d failed: %s", attempt, ex)
            time.sleep(10 * attempt)
    else:
        raise RuntimeError("Overpass unreachable after 4 attempts")
    return [(el["lat"], el["lon"], el["tags"]["name"])
            for el in body.get("elements", []) if el.get("tags", {}).get("name")]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def match(stops: dict[tuple[float, float], str], osm: list[tuple[float, float, str]],
          radius_m: float) -> dict[str, str]:
    """Для каждой остановки датасета — ближайшая OSM в пределах radius_m.
    OSM раскладываем в грид с ячейкой 0.001° (~100м) → ищем в 3×3 соседях."""
    grid: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    for lat, lon, name in osm:
        grid.setdefault((int(lat * 1000), int(lon * 1000)), []).append((lat, lon, name))
    out: dict[str, str] = {}
    for (lat, lon) in stops:
        ki, kj = int(lat * 1000), int(lon * 1000)
        best: tuple[float, str] | None = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for olat, olon, name in grid.get((ki + di, kj + dj), ()):
                    d = _haversine_m(lat, lon, olat, olon)
                    if d <= radius_m and (best is None or d < best[0]):
                        best = (d, name)
        if best:
            out[f"{lat:.5f},{lon:.5f}"] = best[1]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="dataset/validate/schedule_plan.csv", type=Path)
    parser.add_argument("--out", default="backend/app/state/stop_names.json", type=Path)
    parser.add_argument("--radius-m", type=float, default=RADIUS_M)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stops = collect_stops(args.dataset)
    log.info("остановок в датасете (уникальных): %d", len(stops))
    log.info("Overpass: qbox %s ...", BBOX)
    osm = fetch_osm_stops(BBOX)
    log.info("именованных остановок в OSM: %d", len(osm))
    names = match(stops, osm, args.radius_m)
    log.info("совпало (радиус %.0fм): %d / %d", args.radius_m, len(names), len(stops))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    log.info("готово → %s", args.out)


if __name__ == "__main__":
    main()
