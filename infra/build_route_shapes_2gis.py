"""Линии маршрутов для дашборда из 2ГИС: какой реальный маршрут у каждого ТС и его геометрия по дорогам.

1. Для каждого направления ТС (``RouteCatalog``) берём несколько его остановок и спрашиваем 2ГИС (Places API,
   ``type=station_platform``), какие маршруты общественного транспорта через них проходят. Маршрут, который есть
   у большинства остановок направления, — номер этого направления.
2. Запрашиваем маршрут по id (``items/byid``, ``fields=items.directions``) — у каждого его направления есть
   линия по дорогам. Выбираем направление 2ГИС, ближе всего проходящее мимо наших остановок, и обрезаем его по
   нашим первой и последней остановкам.
3. Сравниваем с реальными GPS-треками ТС: берём линию 2ГИС, только если она ближе к треку, чем текущая (OSRM).

Ключ — переменная окружения ``DGIS_API_KEY`` (в код и git не попадает). Ответы 2ГИС кешируются в ``--cache``,
повторный запуск запросы не тратит (лимит бесплатного ключа — 1000 запросов в месяц).

Запуск::

    DGIS_API_KEY=... python infra/build_route_shapes_2gis.py --dataset ./dataset \\
        --out backend/app/api/route_shapes.json --names-out backend/app/api/route_names.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.api.views import RouteCatalog  # noqa: E402
from app.state.schedule import load_schedule  # noqa: E402

API = "https://catalog.api.2gis.com/3.0/"
K = 111_320.0
COS = np.cos(np.radians(55.75))


def xy(lat, lon) -> np.ndarray:
    return np.c_[np.asarray(lon, float) * K * COS, np.asarray(lat, float) * K]


class Dgis:
    def __init__(self, key: str, cache_dir: Path, sleep_s: float = 0.15) -> None:
        self.key, self.cache_dir, self.sleep_s, self.calls = key, cache_dir, sleep_s, 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, path: str, **params) -> dict:
        name = re.sub(r"[^0-9A-Za-z_.,-]+", "_", path + "_" + "_".join(f"{k}={v}" for k, v in sorted(params.items())))
        f = self.cache_dir / f"{name[:180]}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:  # оборванная запись — запросим заново
                f.unlink()
        q = urllib.parse.urlencode({**params, "key": self.key})
        with urllib.request.urlopen(API + path + "?" + q, timeout=30) as r:
            data = json.loads(r.read())
        self.calls += 1
        time.sleep(self.sleep_s)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)  # атомарно: при обрыве не остаётся пустого файла кеша
        return data

    def routes_near(self, lat: float, lon: float, radius: int = 60) -> list[dict]:
        d = self.get("items", point=f"{lon:.6f},{lat:.6f}", radius=radius, type="station_platform",
                     fields="items.routes", page_size=10)
        out = []
        for it in d.get("result", {}).get("items", []):
            out += it.get("routes") or []
        return out

    def route_directions(self, route_id: str) -> list[dict]:
        d = self.get("items/byid", id=route_id, fields="items.directions")
        items = d.get("result", {}).get("items", [])
        return items[0].get("directions", []) if items else []


def parse_linestrings(direction: dict) -> np.ndarray:
    """Направление 2ГИС -> [[lat, lon], ...]: склеиваем отрезки LINESTRING по порядку."""
    pts: list[tuple[float, float]] = []
    for part in direction.get("geometry", {}).get("immersion", []) or []:
        for pair in re.findall(r"(-?\d+\.\d+) (-?\d+\.\d+)", part.get("selection", "")):
            lon, lat = float(pair[0]), float(pair[1])
            if not pts or (abs(pts[-1][0] - lat) > 1e-7 or abs(pts[-1][1] - lon) > 1e-7):
                pts.append((lat, lon))
    return np.array(pts)


def project(poly_xy: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """(расстояние от точки до ломаной, позиция вдоль ломаной в метрах)."""
    a, b = poly_xy[:-1], poly_xy[1:]
    ab = b - a
    L = (ab ** 2).sum(1)
    tt = np.clip(((p - a) * ab).sum(1) / np.where(L > 0, L, 1), 0, 1)
    proj = a + ab * tt[:, None]
    d = np.hypot(*(proj - p).T)
    i = int(d.argmin())
    cum = np.r_[0, np.cumsum(np.sqrt(L))]
    return float(d[i]), float(cum[i] + np.sqrt(L[i]) * tt[i])


def project_after(poly_xy: np.ndarray, p: np.ndarray, min_pos: float) -> tuple[float, float]:
    """Как ``project``, но не даёт спроецироваться раньше ``min_pos`` вдоль линии.

    Нужна, когда маршрут проходит через одно и то же место несколько раз за направление (кольцевые заезды,
    несколько остановок у одного узла типа «Метро Алма-Атинская» на разных витках) — без этого соседние по
    времени остановки могли спроецироваться на РАЗНЫЕ проезды мимо одной точки и линия дёргалась назад-вперёд.
    """
    a, b = poly_xy[:-1], poly_xy[1:]
    ab = b - a
    L = (ab ** 2).sum(1)
    cum = np.r_[0, np.cumsum(np.sqrt(L))]
    tt = np.clip(((p - a) * ab).sum(1) / np.where(L > 0, L, 1), 0, 1)
    proj = a + ab * tt[:, None]
    pos = cum[:-1] + np.sqrt(L) * tt
    d = np.hypot(*(proj - p).T)
    ok = pos >= min_pos - 1e-6
    idx = np.where(ok)[0]
    if len(idx) == 0:  # линия впереди кончилась — берём глобально ближайшую точку
        i = int(d.argmin())
    else:
        i = int(idx[np.argmin(d[idx])])
    return float(d[i]), float(pos[i])


def clip(poly: np.ndarray, start_m: float, end_m: float) -> np.ndarray:
    P = xy(poly[:, 0], poly[:, 1])
    cum = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
    keep = (cum >= start_m) & (cum <= end_m)
    return poly[keep]


def best_direction(polys: list[np.ndarray], S: np.ndarray):
    """Направление 2ГИС, вдоль которого остановки S лежат ближе всего и в том же порядке.

    -> (poly, pos, dist, order) или None; pos — позиции остановок вдоль линии, м.
    """
    best = None
    for poly in polys:
        P = xy(poly[:, 0], poly[:, 1])
        pr = [project(P, s) for s in S]
        dist = float(np.median([p[0] for p in pr]))
        pos = np.array([p[1] for p in pr])
        order = float(np.mean(np.diff(pos) >= -30)) if len(pos) > 1 else 1.0
        score = dist + (1 - order) * 500
        if best is None or score < best[0]:
            best = (score, poly, pos, dist, order)
    return None if best is None else best[1:]


def line_for_stops(polys: list[np.ndarray], S: np.ndarray):
    """Линия 2ГИС для наших остановок. Рейс «туда и обратно» одним куском (кольцевой / с разворотом) делится в
    точке разворота (самая дальняя от начала остановка) на две половины, каждой — своё направление 2ГИС.

    -> (line, dist, order, how) или None.
    """
    b = best_direction(polys, S)
    if b is None:
        return None
    poly, pos, dist, order = b
    if order > 0.8 and pos[-1] > pos[0]:
        return clip(poly, pos[0] - 30, pos[-1] + 30), dist, order, "direct"
    k = int(np.argmax(np.hypot(*(S - S[0]).T)))
    if 2 <= k <= len(S) - 3:
        parts, dists, orders = [], [], []
        for half in (S[:k + 1], S[k:]):
            h = best_direction(polys, half)
            if h is None:
                break
            hp, hpos, hd, ho = h
            if hpos[-1] <= hpos[0]:
                break
            parts.append(clip(hp, hpos[0] - 30, hpos[-1] + 30))
            dists.append(hd)
            orders.append(ho)
        if len(parts) == 2 and all(len(x) >= 2 for x in parts):
            return np.vstack(parts), float(max(dists)), float(min(orders)), "split"
    return (clip(poly, pos[0] - 30, pos[-1] + 30) if pos[-1] > pos[0] else poly), dist, order, "direct"


def _sub(poly: np.ndarray, P: np.ndarray, cum: np.ndarray, a: float, b: float) -> np.ndarray:
    """Кусок ломаной между позициями a < b (м вдоль линии), с точными концами."""
    def at(m):
        i = int(np.clip(np.searchsorted(cum, m) - 1, 0, len(cum) - 2))
        seg = cum[i + 1] - cum[i]
        t = 0.0 if seg <= 0 else (m - cum[i]) / seg
        return poly[i] + (poly[i + 1] - poly[i]) * t
    inner = poly[(cum > a) & (cum < b)]
    return np.vstack([at(a), inner, at(b)])


class Line:
    """Ломаная [[lat, lon]] с предвычисленными метрами — для проекций остановок."""

    def __init__(self, poly: np.ndarray) -> None:
        self.poly = poly
        self.P = xy(poly[:, 0], poly[:, 1])
        self.cum = np.r_[0, np.cumsum(np.hypot(*np.diff(self.P, axis=0).T))]

    def segment(self, s1: np.ndarray, s2: np.ndarray, near_m: float, max_ratio: float):
        d1, p1 = project(self.P, s1)
        d2, p2 = project(self.P, s2)
        straight = float(np.hypot(*(s2 - s1)))
        if d1 > near_m or d2 > near_m or p2 <= p1 or (p2 - p1) > max_ratio * straight + 150:
            return None
        return _sub(self.poly, self.P, self.cum, p1, p2)


def deviation_m(seg: np.ndarray, path: np.ndarray) -> float:
    """Насколько линия ``seg`` отходит от пути ``path``: максимум по точкам seg расстояния до ломаной path, м."""
    Q = xy(path[:, 0], path[:, 1])
    Pts = xy(seg[:, 0], seg[:, 1])
    return max(project(Q, p)[0] for p in Pts) if len(Q) >= 2 else float("inf")


def osrm_pair(cache_dir: Path, s1: np.ndarray, s2: np.ndarray, base_url: str = "https://router.project-osrm.org"
              ) -> np.ndarray | None:
    """Живой маршрут OSRM ровно между двумя точками ``[lat, lon]`` (не кусок из чужой линии — конкретно для этой
    пары). Кешируем на диск: одни и те же перегоны повторяются у многих направлений/перезапусков скрипта."""
    key = f"{s1[0]:.6f},{s1[1]:.6f}_{s2[0]:.6f},{s2[1]:.6f}"
    f = cache_dir / f"osrm_{re.sub(r'[^0-9A-Za-z_.,-]+', '_', key)}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            f.unlink()
            data = None
    else:
        data = None
    if data is None:
        path = f"{s1[1]:.6f},{s1[0]:.6f};{s2[1]:.6f},{s2[0]:.6f}"
        url = f"{base_url}/route/v1/driving/{path}?overview=full&geometries=geojson"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  OSRM недоступен для {key}: {type(e).__name__}", flush=True)
            data = {"code": "Unavailable"}
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        time.sleep(0.3)  # вежливо к публичному API
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return np.array([[lat, lon] for lon, lat in data["routes"][0]["geometry"]["coordinates"]])


def stitch(stops_ll: np.ndarray, S: np.ndarray, dgis: list[Line], osrm: Line | None,
           gps_paths: list | None = None, max_dev_m: float = 60.0, osrm_cache: Path | None = None):
    """Линия по перегонам между соседними остановками.

    Основа — кусок маршрута 2ГИС, если обе остановки на его линии (<= 40 м); если нет — живой маршрут OSRM ровно
    между этими двумя остановками (``osrm_pair``); если и его нет (сеть недоступна) — кусок старой полной линии
    OSRM без «уса» (<= 2.5 прямых расстояния); иначе прямая. Если по GPS известен реальный путь автобуса на этом
    перегоне и основа отходит от него больше чем на ``max_dev_m`` (автобус реально ехал иначе) или основы нет —
    берём реальный путь. -> (line, доли источников).
    """
    parts, src = [], []
    for i in range(len(S) - 1):
        seg, how = None, "straight"
        for ln in dgis:
            seg = ln.segment(S[i], S[i + 1], 40, 3.0)
            if seg is not None:
                how = "2gis"
                break
        if seg is None and osrm_cache is not None:
            r = osrm_pair(osrm_cache, stops_ll[i], stops_ll[i + 1])
            if r is not None and len(r) >= 2:
                seg, how = r, "osrm_live"
        if seg is None and osrm is not None:
            seg = osrm.segment(S[i], S[i + 1], 40, 2.5)
            if seg is not None:
                how = "osrm"
        path = gps_paths[i] if gps_paths is not None else None
        if path is not None and (seg is None or how in ("osrm", "osrm_live") or deviation_m(seg, path) > max_dev_m):
            seg, how = np.vstack([stops_ll[i], path, stops_ll[i + 1]]), "gps"
        if seg is None:
            seg = stops_ll[i:i + 2]
        parts.append(seg if not parts else seg[1:])
        src.append(how)
    line = np.vstack(parts) if parts else stops_ll
    c = Counter(src)
    return line, {k: round(c[k] / max(len(src), 1), 2) for k in ("2gis", "gps", "osrm_live", "osrm", "straight")}


def gps_segment_paths(tr_id: int, direction_id: int, rep_stops: list[dict], sched, cat, tg, arrivals: dict):
    """Реальный путь автобуса по GPS на каждом перегоне направления (или None).

    Берём рейсы ТС этого направления с той же последовательностью остановок, что у эталонного рейса; для
    перегона k — точки трека между прибытием на остановку k и на k+1 (прибытия — ``features.tabular.gps_history``,
    то же правило, что в модели и backend'е). Из рейсов выбираем самый плотный по точкам; скачки GPS (> 108 км/ч)
    отбрасываем.
    """
    key = [(round(s["lat"], 5), round(s["lon"], 5)) for s in rep_stops]
    trips: dict[int, list] = {}
    for v in sched.visits:
        trips.setdefault(v.trip, []).append(v)
    same = [vs for t, vs in trips.items() if cat.direction(tr_id, t) == direction_id
            and [(round(v.lat, 5), round(v.lon, 5)) for v in vs] == key]
    ok = ~np.isnan(tg.lat)
    ts, lat, lon = tg.ts[ok], tg.lat[ok], tg.lon[ok]
    out = []
    for k in range(len(key) - 1):
        best = None
        for vs in same:
            a, b = arrivals.get(vs[k].pos), arrivals.get(vs[k + 1].pos)
            if a is None and b is None:
                continue
            # Прибытие на одну из двух остановок не нашлось (например, автобус ни разу не подъехал к ней ближе
            # ARR_RADIUS — стоп чуть в стороне от реального разворота): берём то же окно поиска, что и travel_gap
            # между рейсами (TRIP_GAP_S = 300 с) от известного конца. Ложный кусок трека отсеет проверка ends ниже.
            a = b - 300 if a is None else a
            b = a + 300 if b is None else b
            if not (0 < b - a < 1800):
                continue
            m = (ts >= a) & (ts <= b)
            if m.sum() < 2:
                continue
            pts = np.c_[lat[m], lon[m]]
            P = xy(pts[:, 0], pts[:, 1])
            step = np.r_[0, np.hypot(*np.diff(P, axis=0).T)]
            dt = np.r_[1, np.diff(ts[m])]
            pts = pts[(step / np.maximum(dt, 1)) <= 30]
            if len(pts) < 2:
                continue
            ends = xy(pts[[0, -1], 0], pts[[0, -1], 1])
            stop_xy = xy([rep_stops[k]["lat"], rep_stops[k + 1]["lat"]], [rep_stops[k]["lon"], rep_stops[k + 1]["lon"]])
            if np.hypot(*(ends - stop_xy).T).max() > 150:  # путь не от этой пары остановок — не берём
                continue
            if best is None or len(pts) > len(best):
                best = pts
        out.append(best)
    return out


def stop_coverage(line: np.ndarray, S: np.ndarray, near_m: float = 60.0) -> float:
    L = xy(line[:, 0], line[:, 1])
    return float(np.mean([project(L, s)[0] <= near_m for s in S])) if len(L) >= 2 else 0.0


def far_share(line: np.ndarray, gps_xy: np.ndarray, far_m: float = 80.0) -> float:
    P = xy(line[:, 0], line[:, 1])
    seg = np.r_[0, np.hypot(*np.diff(P, axis=0).T)]
    dmin = np.concatenate([np.sqrt(((P[i:i + 400, None, :] - gps_xy[None]) ** 2).sum(-1)).min(1)
                           for i in range(0, len(P), 400)])
    return float(seg[dmin > far_m].sum() / max(seg.sum(), 1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, default=Path("dataset"))
    ap.add_argument("--current", type=Path, default=REPO / "backend/app/api/route_shapes.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--names-out", type=Path, default=None, help="номера маршрутов по ТС и направлениям (route_names.json)")
    ap.add_argument("--stop-names-out", type=Path, default=None,
                    help="названия остановок из 2ГИС поверх существующего stop_names.json (ключ «lat,lon», 5 знаков)")
    ap.add_argument("--stop-names-base", type=Path, default=REPO / "backend/app/state/stop_names.json")
    ap.add_argument("--cache", type=Path, default=REPO / ".cache/2gis")
    ap.add_argument("--samples", type=int, default=20, help="остановок на направление для определения маршрута")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    key = os.environ.get("DGIS_API_KEY")
    if not key:
        sys.exit("нужна переменная окружения DGIS_API_KEY")
    dg = Dgis(key, args.cache)

    import pandas as pd
    sys.path.insert(0, str(REPO / "ml/src"))
    from features.tabular import clean_traffic
    cat_schedules = load_schedule(args.dataset / "validate/schedule_plan.csv")
    cat = RouteCatalog(cat_schedules)
    tele = clean_traffic(pd.read_csv(args.dataset / "validate/traffic.csv", parse_dates=["event_time"],
                                     low_memory=False)).dropna(subset=["lat"])
    current = json.loads(args.current.read_text(encoding="utf-8")) if args.current.exists() else {}

    CONFIDENCE_MIN = 0.6  # доля опрошенных остановок, проголосовавших за один и тот же номер маршрута
    COVERAGE_MIN = 0.9    # доля остановок, лежащих на обрезанной линии этого маршрута (<= 60 м)

    shapes: dict[str, dict[str, list]] = {}
    names: dict[str, dict] = {}
    platforms: dict[tuple[float, float], str] = {}   # (lat, lon) остановки 2ГИС -> название
    report = []
    flagged: list[dict] = []  # ТС/направления, для которых НЕ нашли уверенное совпадение с реальным маршрутом
    for tr_id, route in sorted(cat.routes.items()):
        g = tele[tele.tr_id == tr_id]
        gps = xy(g.lat.values, g.lon.values) if len(g) > 50 else None
        shapes[str(tr_id)] = {}
        for d in route["directions"]:
            stops = d["stops"]
            idx = np.unique(np.linspace(1, len(stops) - 2, min(args.samples, max(len(stops) - 2, 1))).round().astype(int))
            votes: Counter = Counter()
            info: dict[str, dict] = {}
            for i in idx:
                seen = set()
                for r in dg.routes_near(stops[i]["lat"], stops[i]["lon"]):
                    if r.get("subtype") not in ("bus", "trolleybus", "shuttle_bus", "express_bus", "electrobus"):
                        continue
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        votes[r["id"]] += 1
                        info[r["id"]] = r
            dir_id = str(d["direction_id"])
            stops_ll = np.array([[st["lat"], st["lon"]] for st in stops])
            row = {"tr_id": tr_id, "dir": dir_id, "stops": len(stops), "samples": len(idx), "route": None, "coverage": 0.0}
            cur = current.get(str(tr_id), {}).get(dir_id)
            if cur and gps is not None:
                row["far_current"] = round(far_share(np.array(cur), gps), 3)

            if not votes:
                row["flag"] = "ни одна из опрошенных остановок не привязана ни к одному маршруту 2ГИС"
                flagged.append(dict(row))
                report.append(row)
                shapes[str(tr_id)][dir_id] = stops_ll.tolist()
                print(row, flush=True)
                continue

            rid, n = votes.most_common(1)[0]
            coverage = n / len(idx)
            row.update(route=info[rid]["name"], coverage=round(coverage, 2),
                       route_from=info[rid].get("from_name"), route_to=info[rid].get("to_name"))

            if coverage < CONFIDENCE_MIN:
                row["flag"] = (f"неуверенно: только {n}/{len(idx)} опрошенных остановок голосуют за "
                               f"«{info[rid]['name']}» — похоже, ТС не идёт по одному цельному реальному маршруту")
                flagged.append(dict(row))
                report.append(row)
                shapes[str(tr_id)][dir_id] = cur if cur else stops_ll.tolist()
                print(row, flush=True)
                continue

            # Маршрут определён уверенно — берём его линию 2ГИС КАК ЕСТЬ (она уже официальная геометрия по
            # дорогам): выбираем направление 2ГИС, ближе всего проходящее мимо наших остановок (с корректным
            # разворотом для рейсов «туда-обратно», см. line_for_stops), обрезаем по первой/последней остановке —
            # и всё, без дальнейшей подгонки по GPS или OSRM. Раньше здесь ещё и «дошивали» линию по перегонам
            # (2ГИС/GPS/OSRM вперемешку) — это и давало зигзаги и самопересечения там, где остановки лежат кучно
            # (несколько заездов к одному узлу за один рейс): для каждого перегона источник выбирался независимо.
            S = xy([s["lat"] for s in stops], [s["lon"] for s in stops])
            rdirs = dg.route_directions(rid)
            for rd in rdirs:
                for pf in rd.get("platforms") or []:
                    m = re.findall(r"-?\d+\.\d+", (pf.get("geometry") or {}).get("centroid", ""))
                    if len(m) >= 2 and pf.get("name"):
                        platforms[(float(m[1]), float(m[0]))] = pf["name"].replace(" ", " ").split(" · ")[0]
            polys = [pl for pl in (parse_linestrings(rd) for rd in rdirs) if len(pl) >= 2]
            found = line_for_stops(polys, S)
            row["base_how"] = found[3] if found is not None else None
            line = found[0] if found is not None and len(found[0]) >= 2 else None
            cov = stop_coverage(line, S) if line is not None else 0.0
            row["stop_coverage"] = round(cov, 2)
            if line is not None and gps is not None:
                row["far_2gis"] = round(far_share(line, gps), 3)

            # Номер маршрута фиксируем уже сейчас — голосование прошло уверенно (см. проверку выше), это не
            # зависит от того, дотянется ли обрезанная линия 2ГИС до всех наших остановок.
            names.setdefault(str(tr_id), {})[dir_id] = {"route": row["route"], "from": row.get("route_from"),
                                                        "to": row.get("route_to"), "coverage": row["coverage"]}
            if line is None or cov < COVERAGE_MIN:
                row["flag"] = (f"маршрут «{info[rid]['name']}» найден уверенно ({n}/{len(idx)}), но его линия "
                               f"2ГИС проходит мимо наших остановок только на {round(cov, 2)} — расхождение в "
                               f"данных остановок, а не в определении номера")
                flagged.append(dict(row))
                shapes[str(tr_id)][dir_id] = cur if cur else stops_ll.tolist()
            else:
                row["used"] = "2gis"
                shapes[str(tr_id)][dir_id] = [[round(a, 6), round(b, 6)] for a, b in line]
            report.append(row)
            print(row, flush=True)

    args.out.write_text(json.dumps(shapes, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if args.names_out:
        out_names = {}
        for tr, dirs in names.items():
            weight = Counter()
            for di, v in dirs.items():
                if v.get("route"):
                    weight[v["route"]] += len(cat.routes[int(tr)]["directions"][int(di)]["stops"])
            ranked = [r for r, _ in weight.most_common()]
            out_names[tr] = {"route_number": ranked[0] if ranked else None, "route_numbers": ranked,
                             "directions": {di: v.get("route") for di, v in dirs.items()}}
        args.names_out.write_text(json.dumps(out_names, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.stop_names_out:
        base = json.loads(args.stop_names_base.read_text(encoding="utf-8")) if args.stop_names_base.exists() else {}
        B = xy([k[0] for k in platforms], [k[1] for k in platforms]) if platforms else np.empty((0, 2))
        bn = list(platforms.values())
        ours = {(round(v.lat, 5), round(v.lon, 5)) for s_ in cat_schedules.values() for v in s_.visits}
        found = 0
        for lat, lon in sorted(ours):
            if len(B):
                d = np.hypot(*(B - xy([lat], [lon])[0]).T)
                j = int(d.argmin())
                if d[j] <= 40:
                    base[f"{lat:.5f},{lon:.5f}"] = bn[j]
                    found += 1
        args.stop_names_out.write_text(json.dumps(base, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"названия остановок: {found} из {len(ours)} точек нашлись в 2ГИС (<= 40 м); всего в файле {len(base)}")
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"запросов к 2ГИС в этом запуске: {dg.calls}; линий: {sum(len(v) for v in shapes.values())} -> {args.out}")
    if flagged:
        print(f"\n=== {len(flagged)} направлений БЕЗ уверенного совпадения с реальным маршрутом 2ГИС "
              f"(линия для них — старая/прямая по остановкам, не 2ГИС): ===")
        for row in flagged:
            print(f"  ТС {row['tr_id']} направление {row['dir']}: {row['flag']}")
    else:
        print("все направления уверенно совпали с реальным маршрутом 2ГИС")


if __name__ == "__main__":
    main()
