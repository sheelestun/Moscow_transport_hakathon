"""Симулятор движения ТС по маршрутам — Python-порт логики frontend/js/mock.js.

Держит N ТС на каждом маршруте. У маршрута — 1..2 направления с собственной
трассой (``line``) и остановками (``stops``); маршрут «Б» — кольцевой (одно
направление, ТС продолжает по кругу). Каждый тик: сдвигаем позицию по трассе,
пересчитываем ``delay_now_sec``, ``delay_pred_sec``, ``risk_score`` и
диагностические поля (``data_status``, ``p_early/p_ontime/p_late``,
``delay_interval_sec``, ``horizon_ok``). Локальный прогноз — фолбэк на случай,
когда ML недоступен; при доступности бэкенд может подменить его ответом ML.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from routes_data import ROUTES, SIGNALS


PLAN_SPEED_MPS = 18 / 3.6  # 5 м/с — средняя плановая скорость с учётом остановок
HORIZON_S = 12.5 * 60      # середина окна прогноза 10–15 мин
RISK_MID_SEC = 120.0
RISK_SLOPE_SEC = 60.0
RISK_RED = 0.7

REASONS = ["traffic_jam_ahead", "long_dwell", "speed_drop", "bunching", "accumulated_delay"]
REC_FOR_REASON = {
    "traffic_jam_ahead": "detour",
    "long_dwell": "adjust_interval",
    "speed_drop": "signal_priority",
    "bunching": "hold_at_stop",
    "accumulated_delay": "add_reserve",
}
FEATURES_FOR_REASON = {
    "traffic_jam_ahead": ["traffic_score", "speed_avg_5min", "cur_dev_s", "hour_of_day"],
    "long_dwell": ["dwell_last_stop_sec", "cur_dev_s", "hour_of_day", "headway_to_prev_sec"],
    "speed_drop": ["speed_avg_5min", "speed_avg_15min", "distance_to_target_m", "cur_dev_s"],
    "bunching": ["headway_to_next_sec", "headway_to_prev_sec", "cur_dev_s", "speed_avg_5min"],
    "accumulated_delay": ["cur_dev_s", "current_delay_sec", "planned_time_to_target_sec", "traffic_score"],
}


def risk_from_delay(delay: float) -> float:
    return 1.0 / (1.0 + math.exp(-(delay - RISK_MID_SEC) / RISK_SLOPE_SEC))


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


@dataclass
class DirectionRuntime:
    """Одно направление маршрута: линия + остановки, спроецированные на pos_m."""
    direction_id: int
    name: str
    osm_name: str
    line: list[list[float]]
    stops: list[dict]
    cum_m: list[float]
    length_m: float

    def point_at(self, pos_m: float) -> tuple[float, float]:
        pos_m = max(0.0, min(pos_m, self.length_m))
        for i in range(len(self.cum_m) - 1):
            if self.cum_m[i + 1] >= pos_m:
                seg = self.cum_m[i + 1] - self.cum_m[i]
                t = 0.0 if seg <= 0 else (pos_m - self.cum_m[i]) / seg
                a, b = self.line[i], self.line[i + 1]
                return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        last = self.line[-1]
        return last[0], last[1]


@dataclass
class SignalRuntime:
    """Светофор с фиксированным циклом/фазой (детерминированный сид от координат).

    Реальные фазы у ЦОДД — при промышленном внедрении подставляются оттуда.
    """
    signal_id: str
    lat: float
    lon: float
    cycle: float
    green: float
    offset: float


@dataclass
class RouteRuntime:
    """Маршрут с посчитанными направлениями и метаданными."""
    route_id: str
    transport_type: str
    name: str
    loop: bool
    dirs: list[DirectionRuntime]
    signals: list[SignalRuntime]
    _priority_until_ms: int = 0  # включён «signal_priority» для маршрута до этого времени

    @classmethod
    def build(cls, raw: dict, signals: list[list[float]] | None = None) -> "RouteRuntime":
        dirs_out: list[DirectionRuntime] = []
        for i, d in enumerate(raw["dirs"]):
            line = d["line"]
            cum = [0.0]
            for j in range(1, len(line)):
                cum.append(cum[-1] + haversine_m(line[j - 1], line[j]))
            stops_out: list[dict] = []
            for idx, s in enumerate(d["stops"]):
                name, lat, lon = s[0], s[1], s[2]
                pos_m = _project(line, cum, (lat, lon))
                stops_out.append({
                    "stop_id": f"{raw['route_id']}-{i}-{idx + 1}",
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "pos_m": pos_m,
                })
            stops_out.sort(key=lambda x: x["pos_m"])
            # имя направления: «X → Y» из первой и последней остановки
            dir_name = d.get("name") or (
                f"{stops_out[0]['name']} → {stops_out[-1]['name']}"
                if stops_out else raw["route_id"]
            )
            dirs_out.append(DirectionRuntime(
                direction_id=i, name=dir_name, osm_name=d.get("osm_name", ""),
                line=line, stops=stops_out, cum_m=cum, length_m=cum[-1],
            ))
        sig_runtime: list[SignalRuntime] = []
        for i, (lat, lon) in enumerate(signals or []):
            seed = abs(math.sin((lat * 1e4 + lon * 1e4) * 12.9898)) * 1000
            cycle = 80 + (seed % 20)
            green = cycle * (0.6 + (seed % 12) / 100)
            offset = seed % cycle
            sig_runtime.append(SignalRuntime(
                signal_id=f"{raw['route_id']}-s{i + 1}", lat=lat, lon=lon,
                cycle=cycle, green=green, offset=offset,
            ))
        return cls(
            route_id=raw["route_id"], transport_type=raw["transport_type"], name=raw["name"],
            loop=bool(raw.get("loop", False)), dirs=dirs_out, signals=sig_runtime,
        )


def _project(line: list[list[float]], cum: list[float], pt: tuple[float, float]) -> float:
    """Проекция точки на polyline: возвращает pos_m ближайшей точки."""
    best_pos = 0.0
    best_d = float("inf")
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        ax, ay = a[1], a[0]  # lon, lat
        bx, by = b[1], b[0]
        px, py = pt[1], pt[0]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        proj = (ay + dy * t, ax + dx * t)  # lat, lon
        d = haversine_m(proj, pt)
        if d < best_d:
            best_d = d
            seg_len = cum[i + 1] - cum[i]
            best_pos = cum[i] + seg_len * t
    return best_pos


@dataclass
class Vehicle:
    vehicle_id: str
    route_id: str
    direction_id: int        # индекс в dirs
    pos_m: float             # 0..dirs[direction_id].length_m, только вперёд
    speed_kmh: float
    speed_avg: float
    anchor_ms: int           # ms эпохи для начала рейса (плановое время в точке 0)
    trip_id: int
    trouble_left_s: float = 0.0
    trouble_speed_kmh: float = 12.0
    reason: str = "accumulated_delay"
    features: list[dict] = field(default_factory=list)
    confidence: float = 0.75
    noise: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    delay_now_s: float = 0.0
    delay_pred_s: int = 0
    risk_score: float = 0.0
    updated_at: str = ""
    is_reserve: bool = False
    recover_left_s: float = 0.0
    facts: dict[str, float] = field(default_factory=dict)  # stop_id → ms фактического прохождения
    last_trip: Optional[dict] = None
    alerted_stops: set[str] = field(default_factory=set)
    alerted_trip: int = -1
    # диагностика/интервал (мокируются симулятором, при live-ML заменяются реальными)
    data_status: str = "live"       # live | stale | off_route | no_telemetry | fallback
    off_route_m: Optional[float] = None
    p_early: float = 0.0
    p_ontime: float = 1.0
    p_late: float = 0.0
    delay_interval_sec: Optional[list[int]] = None  # [q10, q90] с 80%-покрытием


def _plan_time_ms(v: Vehicle, along_m: float) -> float:
    return v.anchor_ms + (along_m / PLAN_SPEED_MPS) * 1000


def _make_features(reason: str) -> list[dict]:
    weights = [random.uniform(0.30, 0.45), random.uniform(0.15, 0.25),
               random.uniform(0.08, 0.15), random.uniform(0.03, 0.08)]
    return [{"name": n, "contribution": round(w, 2)}
            for n, w in zip(FEATURES_FOR_REASON[reason], weights)]


class Simulator:
    """Один инстанс на процесс. `step(dt)` — сдвиг симуляции на `dt` секунд симуляции."""

    def __init__(self, speed_factor: float = 5.0, seed: int = 42) -> None:
        random.seed(seed)
        self.routes: dict[str, RouteRuntime] = {}
        for raw in ROUTES:
            r = RouteRuntime.build(raw, signals=SIGNALS.get(raw["route_id"], []))
            self.routes[r.route_id] = r
        self.vehicles: list[Vehicle] = []
        self.by_id: dict[str, Vehicle] = {}
        self._trip_seq = 0
        self._reserve_seq = 1
        self._sim_now_ms = int(time.time() * 1000)
        self.speed_factor = float(speed_factor)
        self.offline = False
        self._init_fleet()

    # ---------- инициализация ----------

    def _init_fleet(self) -> None:
        for r in self.routes.values():
            for i in range(4):
                d_idx = i % len(r.dirs)
                d = r.dirs[d_idx]
                pos = d.length_m * ((i // max(1, len(r.dirs))) + random.uniform(0.15, 0.8)) / max(
                    1, (4 + len(r.dirs) - 1) // len(r.dirs))
                pos = max(0.0, min(pos, d.length_m * 0.98))
                v = Vehicle(
                    vehicle_id=str(random.randint(120000, 139999)),
                    route_id=r.route_id,
                    direction_id=d_idx,
                    pos_m=pos,
                    speed_kmh=random.uniform(17, 22),
                    speed_avg=18.0,
                    anchor_ms=0,
                    trip_id=0,
                    features=_make_features("accumulated_delay"),
                    confidence=round(random.uniform(0.60, 0.85), 2),
                )
                self._new_trip(v, delay_s=random.uniform(-40, 70))
                self.vehicles.append(v)
                self.by_id[v.vehicle_id] = v
        # несколько ТС «в проблеме» с первой секунды
        for idx, k in enumerate((1, 5, 9, 13, 17)):
            if k < len(self.vehicles):
                v = self.vehicles[k]
                self._new_trip(v, delay_s=random.uniform(110, 170))
                self._start_trouble(v, strength=0.7 if idx == 3 else 1.0)
                v.trouble_left_s = random.uniform(300, 700)

    def _new_trip(self, v: Vehicle, delay_s: float) -> None:
        d = self._dir(v)
        if v.facts:
            v.last_trip = {"trip_id": v.trip_id, "facts": dict(v.facts),
                           "anchor_ms": v.anchor_ms, "direction_id": v.direction_id}
        self._trip_seq += 1
        v.trip_id = self._trip_seq
        v.noise = random.uniform(-40, 40)
        v.facts = {}
        v.anchor_ms = int(self._sim_now_ms - delay_s * 1000 - (v.pos_m / PLAN_SPEED_MPS) * 1000)

    def _start_trouble(self, v: Vehicle, strength: float = 1.0) -> None:
        v.trouble_left_s = random.uniform(240, 720)
        v.reason = random.choice([r for r in REASONS if r != "accumulated_delay"])
        v.trouble_speed_kmh = random.uniform(7, 12) / max(strength, 0.1)
        v.noise = random.uniform(-70, 70)
        v.features = _make_features(v.reason)
        v.confidence = round(random.uniform(0.62, 0.90), 2)

    def _dir(self, v: Vehicle) -> DirectionRuntime:
        return self.routes[v.route_id].dirs[v.direction_id]

    # ---------- симуляция ----------

    def step(self, dt_s: float) -> None:
        self._sim_now_ms += int(dt_s * 1000)
        now_ms = self._sim_now_ms
        for v in self.vehicles:
            r = self.routes[v.route_id]
            d = self._dir(v)
            if v.trouble_left_s > 0:
                v.trouble_left_s -= dt_s
                v.speed_kmh = max(2.0, min(14.0, v.trouble_speed_kmh + random.uniform(-1.5, 1.5)))
            else:
                if v.reason != "accumulated_delay":
                    v.reason = "accumulated_delay"
                    v.features = _make_features(v.reason)
                p = 0.001 * math.sqrt(self.speed_factor / 5.0) * dt_s / max(self.speed_factor, 0.5)
                if random.random() < p:
                    self._start_trouble(v)
                target = 22 if v.delay_now_s > 20 else (14 if v.delay_now_s < -20 else 18)
                v.speed_kmh = max(10.0, min(28.0,
                                            v.speed_kmh + (target - v.speed_kmh) * 0.3 + random.uniform(-1, 1)))
            v.speed_avg += (v.speed_kmh - v.speed_avg) * 0.08 * dt_s

            if v.recover_left_s > 0:
                dd = min(v.recover_left_s, 1.2 * dt_s)
                v.anchor_ms += int(dd * 1000)
                v.recover_left_s -= dd

            before = v.pos_m
            v.pos_m = min(d.length_m, v.pos_m + (v.speed_kmh / 3.6) * dt_s)
            after = v.pos_m
            for s in d.stops:
                if before < s["pos_m"] <= after + 0.5:
                    v.facts[s["stop_id"]] = now_ms
            if v.pos_m >= d.length_m:
                # конец направления: у кольцевого — тот же, у 2-дир — следующее
                v.direction_id = (v.direction_id + 1) % len(r.dirs)
                v.pos_m = 0.0
                self._new_trip(v, delay_s=random.uniform(-30, 60))

            d = self._dir(v)
            v.lat, v.lon = d.point_at(v.pos_m)
            v.delay_now_s = (now_ms - _plan_time_ms(v, v.pos_m)) / 1000
            v.delay_pred_s = int(max(-300, min(900, self._predict_delay(v, HORIZON_S))))
            v.risk_score = round(risk_from_delay(v.delay_pred_s), 3)
            v.updated_at = _iso_ms(now_ms)

            # диагностика: p_early/ontime/late, интервал ±90 с (мок)
            self._update_uncertainty(v)

    def _future_delay(self, v: Vehicle, t: float) -> float:
        d = v.delay_now_s
        tl = max(0.0, v.trouble_left_s)
        tr = min(t, tl)
        if tr > 0:
            d += (1 - (v.trouble_speed_kmh / 3.6) / PLAN_SPEED_MPS) * tr
        rest = t - tr
        if v.recover_left_s > 0:
            d -= min(v.recover_left_s, 1.2 * t)
        catch_up = 1 - (22 / 3.6) / PLAN_SPEED_MPS
        if d > 0:
            d = max(0, d + catch_up * rest)
        else:
            d = min(0, d - catch_up * rest)
        return d

    def _predict_delay(self, v: Vehicle, t: float) -> float:
        """Локальный прогноз с шумом (как у настоящей модели MAE~40-60 с)."""
        return self._future_delay(v, t) + v.noise * min(1.0, t / HORIZON_S)

    def _update_uncertainty(self, v: Vehicle) -> None:
        """Моделирует то, что настоящая ML отдаёт как p_early/ontime/late + интервал.

        Классы: early ≤ −60с, ontime (−60..+120с), late > +120с. Ширина интервала
        зависит от confidence: чем ниже — тем шире (пропорция ±90с при conf=0.7).
        """
        d = float(v.delay_pred_s)
        margin = round(90 * (1.2 - min(1.0, max(0.3, v.confidence))))
        v.delay_interval_sec = [int(d - margin), int(d + margin)]
        # softmax-подобная раскладка вероятностей вокруг центра
        p_late = 1.0 / (1.0 + math.exp(-(d - 120) / 60))
        p_early = 1.0 / (1.0 + math.exp(-(-60 - d) / 60))
        p_ontime = max(0.0, 1.0 - p_late - p_early)
        s = p_late + p_early + p_ontime
        v.p_late = round(p_late / s, 3)
        v.p_early = round(p_early / s, 3)
        v.p_ontime = round(p_ontime / s, 3)
        # data_status: у ТС с проблемой ниже уверенность — иногда «stale»
        if v.confidence < 0.5:
            v.data_status = "stale"
        else:
            v.data_status = "live"
        v.off_route_m = None  # симуляция всегда на маршруте

    # ---------- публичные вьюхи ----------

    def get_routes(self) -> list[dict]:
        out: list[dict] = []
        for r in self.routes.values():
            d0 = r.dirs[0]
            item = {
                "route_id": r.route_id, "name": r.name, "transport_type": r.transport_type,
                "loop": r.loop,
                # backward-compat: geometry/stops от первого направления
                "geometry": d0.line,
                "stops": [{"stop_id": s["stop_id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"]}
                          for s in d0.stops],
                # полный список направлений — фронт использует это
                "directions": [{
                    "direction_id": d.direction_id, "name": d.name, "geometry": d.line,
                    "stops": [{"stop_id": s["stop_id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"]}
                              for s in d.stops],
                } for d in r.dirs],
                "signals_count": len(r.signals),
            }
            out.append(item)
        return out

    def get_signals(self, route_id: str) -> list[dict]:
        """Светофоры маршрута с текущей фазой (green/red/priority) + сколько секунд осталось."""
        r = self.routes.get(route_id)
        if not r:
            return []
        now_ms = self._sim_now_ms
        priority = r._priority_until_ms > now_ms
        out: list[dict] = []
        for sg in r.signals:
            if priority:
                state = "priority"
                left = int(max(0, (r._priority_until_ms - now_ms) / 1000))
            else:
                t = (now_ms / 1000 + sg.offset) % sg.cycle
                if t < sg.green:
                    state, left = "green", int(sg.green - t)
                else:
                    state, left = "red", int(sg.cycle - t)
            out.append({"signal_id": sg.signal_id, "lat": sg.lat, "lon": sg.lon,
                        "state": state, "left": left, "cycle": round(sg.cycle),
                        "green": round(sg.green)})
        return out

    def _headway_prev_sec(self, v: Vehicle) -> tuple[Optional[int], Optional[int]]:
        """Сколько секунд назад через текущую точку `v` прошёл предыдущий ТС того же
        маршрута/направления. Возвращает (headway, plan_headway). Оба — целые секунды.

        Для интерактивной карточки: если фактический интервал заметно меньше
        планового — bus bunching (Daganzo, 2009). Диспетчер видит это сразу.
        """
        if v.is_reserve:
            return None, None
        peers = [x for x in self.vehicles if not x.is_reserve
                 and x.route_id == v.route_id and x.direction_id == v.direction_id]
        if len(peers) < 2:
            return None, None
        peers.sort(key=lambda x: x.pos_m)
        idx = next((i for i, x in enumerate(peers) if x.vehicle_id == v.vehicle_id), None)
        if idx is None or idx == 0:
            return None, None
        prev = peers[idx - 1]
        headway = int((v.pos_m - prev.pos_m) / PLAN_SPEED_MPS)
        d = self.routes[v.route_id].dirs[v.direction_id]
        plan_headway = int(d.length_m / (len(peers) * PLAN_SPEED_MPS))
        return headway, plan_headway

    def pub_vehicle(self, v: Vehicle) -> dict:
        headway, plan_headway = self._headway_prev_sec(v)
        out = {
            "vehicle_id": v.vehicle_id, "route_id": v.route_id, "direction_id": v.direction_id,
            "lat": v.lat, "lon": v.lon,
            "is_reserve": v.is_reserve,
            "speed": round(v.speed_kmh), "heading": None,
            "delay_now_sec": round(v.delay_now_s), "delay_pred_sec": v.delay_pred_s,
            "risk_score": v.risk_score, "updated_at": v.updated_at,
            "confidence": v.confidence,
            "data_status": v.data_status,
            "p_early": v.p_early, "p_ontime": v.p_ontime, "p_late": v.p_late,
            "delay_interval_sec": v.delay_interval_sec,
            "headway_prev_sec": headway,
            "plan_headway_sec": plan_headway,
        }
        if v.off_route_m is not None:
            out["off_route_m"] = v.off_route_m
        if v.risk_score >= 0.35:  # yellow+red — фронт ждёт reason/rec для не-зелёных
            out.update({
                "reason_pattern": v.reason,
                "recommendation": REC_FOR_REASON.get(v.reason, "monitor"),
                "top_features": v.features,
            })
        return out

    def get_vehicles(self) -> list[dict]:
        return [self.pub_vehicle(v) for v in self.vehicles]

    def get_schedule(self, vehicle_id: str) -> dict:
        v = self.by_id.get(vehicle_id)
        if v is None:
            raise KeyError(vehicle_id)
        d = self._dir(v)
        rows: list[dict] = []
        for s in d.stops:
            plan_ms = _plan_time_ms(v, s["pos_m"])
            row: dict = {"stop_id": s["stop_id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                         "time_plan": _iso_ms(int(plan_ms))}
            if s["pos_m"] <= v.pos_m:
                fact_ms = v.facts.get(s["stop_id"])
                if fact_ms is None:
                    frac = 0.4 + 0.6 * (s["pos_m"] / max(v.pos_m, 1.0))
                    fact_ms = plan_ms + v.delay_now_s * 1000 * frac
                row.update({
                    "status": "passed",
                    "time_fact": _iso_ms(int(fact_ms)),
                    "delay_sec": round((fact_ms - plan_ms) / 1000),
                })
            else:
                ahead = (s["pos_m"] - v.pos_m) / PLAN_SPEED_MPS
                delay = self._predict_delay(v, ahead)
                status = "next" if not any(x["status"] == "next" for x in rows) else "upcoming"
                row.update({
                    "status": status,
                    "time_pred": _iso_ms(int(plan_ms + delay * 1000)),
                    "delay_sec": round(delay),
                    "_ahead": ahead,
                })
            rows.append(row)
        upcoming = [x for x in rows if x["status"] != "passed"]
        target = None
        for x in upcoming:
            if 600 < x["_ahead"] <= 900:
                target = x
                break
        if target is None and upcoming:
            target = min(upcoming, key=lambda x: abs(x["_ahead"] - HORIZON_S))
        if target is not None:
            target["is_target"] = True
        for x in rows:
            x.pop("_ahead", None)
        return {
            "vehicle_id": v.vehicle_id, "route_id": v.route_id,
            "direction_id": v.direction_id,
            "direction": d.name,
            "stops": rows,
        }

    # ---------- аналитика ----------

    def get_worst_stops(self, limit: int = 10) -> list[dict]:
        """Топ-N проблемных остановок «прямо сейчас» — где ТС ждут наибольших опозданий.

        Идея: обходим всех «живых» ТС; для каждой предстоящей остановки достаём
        прогноз задержки; группируем по (route_id, direction_id, stop_id). Ранжируем
        по средней задержке — эти места диспетчер видит сразу, а не после жалобы.
        """
        agg: dict[tuple[str, int, str], dict] = {}
        for v in self.vehicles:
            if v.is_reserve:
                continue
            d = self._dir(v)
            for s in d.stops:
                if s["pos_m"] <= v.pos_m:
                    continue
                ahead = (s["pos_m"] - v.pos_m) / PLAN_SPEED_MPS
                if ahead > 15 * 60:  # смотрим только ближайшие 15 минут
                    continue
                delay = self._predict_delay(v, ahead)
                key = (v.route_id, v.direction_id, s["stop_id"])
                cell = agg.get(key)
                if cell is None:
                    cell = {"route_id": v.route_id, "direction_id": v.direction_id,
                            "stop_id": s["stop_id"], "name": s["name"],
                            "lat": s["lat"], "lon": s["lon"],
                            "vehicles": 0, "sum_delay": 0.0, "max_delay": 0.0}
                    agg[key] = cell
                cell["vehicles"] += 1
                cell["sum_delay"] += delay
                if delay > cell["max_delay"]:
                    cell["max_delay"] = delay
        rows = []
        for cell in agg.values():
            avg = cell["sum_delay"] / cell["vehicles"]
            if avg < 30:  # меньше 30 с — не «проблемная» точка
                continue
            rows.append({
                "route_id": cell["route_id"],
                "direction_id": cell["direction_id"],
                "stop_id": cell["stop_id"],
                "name": cell["name"],
                "lat": cell["lat"], "lon": cell["lon"],
                "avg_delay_sec": round(avg),
                "max_delay_sec": round(cell["max_delay"]),
                "vehicles": cell["vehicles"],
            })
        rows.sort(key=lambda r: (-r["avg_delay_sec"], -r["vehicles"]))
        return rows[:limit]

    def get_bunching(self, factor: float = 0.6, min_gap_sec: float = 45.0) -> list[dict]:
        """Bus bunching (Daganzo, 2009): пары соседних ТС одного маршрута/направления,
        у которых фактический интервал меньше `factor` · планового.

        План считаем как `длина_направления / (n_ТС_в_направлении · plan_speed)`.
        Если факт-интервал < max(min_gap_sec, factor·плана) — попадает в список.
        Диспетчер сразу видит, где «слипаются» — можно применить hold_at_stop.
        """
        groups: dict[tuple[str, int], list[Vehicle]] = {}
        for v in self.vehicles:
            if v.is_reserve:
                continue
            groups.setdefault((v.route_id, v.direction_id), []).append(v)
        out: list[dict] = []
        for (route_id, dir_id), vs in groups.items():
            if len(vs) < 2:
                continue
            d = self.routes[route_id].dirs[dir_id]
            plan_headway = d.length_m / (len(vs) * PLAN_SPEED_MPS)
            threshold = max(min_gap_sec, plan_headway * factor)
            vs_sorted = sorted(vs, key=lambda x: x.pos_m)
            for a, b in zip(vs_sorted[:-1], vs_sorted[1:]):
                headway = (b.pos_m - a.pos_m) / PLAN_SPEED_MPS
                if headway < threshold:
                    out.append({
                        "route_id": route_id,
                        "direction_id": dir_id,
                        "leader_id": a.vehicle_id,
                        "follower_id": b.vehicle_id,
                        "headway_sec": round(headway),
                        "plan_headway_sec": round(plan_headway),
                        "ratio": round(headway / max(plan_headway, 1.0), 2),
                        "leader": {"lat": a.lat, "lon": a.lon},
                        "follower": {"lat": b.lat, "lon": b.lon},
                    })
        out.sort(key=lambda x: x["ratio"])
        return out

    # ---------- what-if ----------

    def measure_effect(self, v: Vehicle, scenario: str) -> int:
        """Локальный сценарный сдвиг (MEASURE_EFFECT из mock.js)."""
        effect = {"add_reserve": 0.30, "adjust_interval": 0.25, "detour": 0.25,
                  "signal_priority": 0.25, "hold_at_stop": 0.20}
        fits = {"add_reserve": ["accumulated_delay", "long_dwell"],
                "adjust_interval": ["bunching", "long_dwell"],
                "detour": ["traffic_jam_ahead"],
                "signal_priority": ["speed_drop", "traffic_jam_ahead"],
                "hold_at_stop": ["bunching"]}
        before = v.delay_pred_s
        if before <= 30:
            return before
        k = effect.get(scenario, 0.0)
        if REC_FOR_REASON.get(v.reason) == scenario:
            k += 0.45
        elif v.reason in fits.get(scenario, ()):
            k += 0.20
        h = sum(ord(c) for c in v.vehicle_id) % 10
        k = min(0.90, k * (0.90 + h / 50))
        return round(before * (1 - k))

    def apply_measure(self, route_id: str, scenario: str) -> dict:
        for v in [x for x in self.vehicles if x.route_id == route_id]:
            gain = v.delay_pred_s - self.measure_effect(v, scenario)
            if gain > 0:
                v.recover_left_s += max(0.0, v.delay_now_s) * (gain / max(v.delay_pred_s, 1))
            fits = {"detour": ["traffic_jam_ahead"], "signal_priority": ["speed_drop", "traffic_jam_ahead"],
                    "hold_at_stop": ["bunching"], "adjust_interval": ["bunching", "long_dwell"],
                    "add_reserve": ["accumulated_delay", "long_dwell"]}
            if v.reason in fits.get(scenario, ()):
                v.trouble_left_s = 0
        if scenario == "add_reserve":
            self._add_reserve(route_id)
        if scenario == "signal_priority":
            self.routes[route_id]._priority_until_ms = self._sim_now_ms + 20 * 60 * 1000
        return {"ok": True}

    def _add_reserve(self, route_id: str) -> Vehicle:
        r = self.routes[route_id]
        v = Vehicle(
            vehicle_id=f"Р{self._reserve_seq}-{route_id}",
            route_id=route_id,
            direction_id=0,
            pos_m=0.0,
            speed_kmh=22.0,
            speed_avg=20.0,
            anchor_ms=0,
            trip_id=0,
            features=_make_features("accumulated_delay"),
            confidence=0.80,
            is_reserve=True,
        )
        self._reserve_seq += 1
        self._new_trip(v, delay_s=-20)
        self.vehicles.append(v)
        self.by_id[v.vehicle_id] = v
        self.step(0.0)
        return v

    def now_ms(self) -> int:
        return self._sim_now_ms


def _iso_ms(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
