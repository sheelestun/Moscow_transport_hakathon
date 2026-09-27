"""Прогоняем плановые остановки через публичный OSRM и кладём road-geometry в JSON,
чтобы дашборд рисовал маршрут по улицам, а не прямыми stop-to-stop.

Датасет road-shape не поставляет (см. `backend/app/api/views.py:6`), поэтому раз в
жизнь просим `router.project-osrm.org` соединить остановки трипа по дорогам,
кэшируем ответ в `backend/app/api/route_shapes.json` (~5-20 KB) и на старте
`RouteCatalog` подмешивает эту линию вместо ломаной по остановкам. Фронт менять
не надо — контракт `direction.geometry = [[lat, lon], ...]` тот же.

Один запуск, коммитим результат в git. Пере-собирать нужно только если поменялся
`schedule_plan.csv`.

Запуск:
    python infra/build_route_shapes.py --dataset ./dataset/validate/schedule_plan.csv

Ключи:
    --osrm       OSRM base URL (default: публичный router.project-osrm.org)
    --out        куда писать JSON (default: backend/app/api/route_shapes.json)
    --sleep      пауза между запросами, сек (default: 0.5 — вежливо к публичному API)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.api.views import RouteCatalog  # noqa: E402
from app.state.schedule import load_schedule  # noqa: E402

log = logging.getLogger("build_route_shapes")


def osrm_route(base_url: str, coords: list[tuple[float, float]]) -> list[list[float]]:
    """coords: [(lat, lon), ...] → [[lat, lon], ...] по дорогам. Пусто если OSRM не смог."""
    path = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    url = f"{base_url.rstrip('/')}/route/v1/driving/{path}?overview=full&geometries=geojson"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    if data.get("code") != "Ok" or not data.get("routes"):
        log.warning("OSRM %s: %s", data.get("code"), data.get("message"))
        return []
    return [[lat, lon] for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


def build(schedule_path: Path, osrm_url: str, sleep_s: float) -> dict[str, dict[str, list[list[float]]]]:
    schedules = load_schedule(schedule_path)
    catalog = RouteCatalog(schedules)
    shapes: dict[str, dict[str, list[list[float]]]] = {}
    total = sum(len(r["directions"]) for r in catalog.routes.values())
    done = 0
    for tr_id, route in catalog.routes.items():
        shapes[str(tr_id)] = {}
        for d in route["directions"]:
            done += 1
            stops = d["geometry"]
            if len(stops) < 2:
                log.info("[%d/%d] tr_id=%s dir=%d: %d stops, пропускаем", done, total, tr_id, d["direction_id"], len(stops))
                continue
            try:
                shape = osrm_route(osrm_url, stops)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                log.warning("[%d/%d] tr_id=%s dir=%d: %s — fallback на stop-to-stop", done, total, tr_id, d["direction_id"], e)
                shape = []
            if shape:
                shapes[str(tr_id)][str(d["direction_id"])] = shape
                log.info("[%d/%d] tr_id=%s dir=%d: %d stops → %d road-points", done, total, tr_id, d["direction_id"], len(stops), len(shape))
            time.sleep(sleep_s)
    return shapes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="dataset/validate/schedule_plan.csv", type=Path)
    parser.add_argument("--osrm", default="https://router.project-osrm.org")
    parser.add_argument("--out", default="backend/app/api/route_shapes.json", type=Path)
    parser.add_argument("--sleep", type=float, default=0.5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    shapes = build(args.dataset, args.osrm, args.sleep)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(shapes, ensure_ascii=False, separators=(",", ":")))
    dirs = sum(len(v) for v in shapes.values())
    log.info("готово: %d маршрутов, %d directions, → %s (%d байт)",
             len(shapes), dirs, args.out, args.out.stat().st_size)


if __name__ == "__main__":
    main()
