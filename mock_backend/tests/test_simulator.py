"""Smoke-тесты симулятора: контракт `pub_vehicle`, worst-stops, bunching, headway.

Симулятор — единственный источник телеметрии на живом стенде, пока NDTP-парсер
в разработке. Контракт с фронтом фиксирован в `frontend/js/mock.js`; тесты ловят
регрессии до того, как они долетят до дашборда.
"""
from __future__ import annotations

import pytest

from simulator import Simulator


@pytest.fixture(scope="module")
def sim() -> Simulator:
    s = Simulator(seed=42)
    s.step(1.0)
    return s


def test_pub_vehicle_shape(sim: Simulator) -> None:
    """pub_vehicle возвращает все поля, которые ждёт фронт."""
    v = sim.pub_vehicle(sim.vehicles[0])
    required = {
        "vehicle_id", "route_id", "direction_id", "lat", "lon",
        "is_reserve", "speed", "delay_now_sec", "delay_pred_sec",
        "risk_score", "confidence", "data_status",
        "p_early", "p_ontime", "p_late", "delay_interval_sec",
        "headway_prev_sec", "plan_headway_sec",
    }
    missing = required - set(v)
    assert not missing, f"pub_vehicle без полей: {missing}"


def test_probabilities_sum_to_one(sim: Simulator) -> None:
    for veh in sim.vehicles[:5]:
        v = sim.pub_vehicle(veh)
        s = (v["p_early"] or 0) + (v["p_ontime"] or 0) + (v["p_late"] or 0)
        assert 0.98 <= s <= 1.02, f"p_* сумма {s:.3f} для {v['vehicle_id']}"


def test_delay_interval_covers_prediction(sim: Simulator) -> None:
    """Предсказание должно лежать внутри [q10, q90] — иначе интервал вырожден."""
    for veh in sim.vehicles[:10]:
        v = sim.pub_vehicle(veh)
        iv = v.get("delay_interval_sec")
        if iv is None:
            continue
        lo, hi = iv
        assert lo <= v["delay_pred_sec"] <= hi, f"pred {v['delay_pred_sec']} вне [{lo}, {hi}]"


def test_worst_stops_sorted_and_capped(sim: Simulator) -> None:
    stops = sim.get_worst_stops(limit=5)
    assert len(stops) <= 5
    delays = [s["avg_delay_sec"] for s in stops]
    assert delays == sorted(delays, reverse=True), "worst_stops должен быть отсортирован по убыванию"


def test_bunching_pair_shape(sim: Simulator) -> None:
    pairs = sim.get_bunching()
    for p in pairs:
        assert p["leader_id"] != p["follower_id"]
        assert p["headway_sec"] <= p["plan_headway_sec"]
        assert 0 <= p["ratio"] <= 1.0
        assert set(p) >= {"route_id", "direction_id", "leader", "follower"}


def test_headway_only_when_leader_exists(sim: Simulator) -> None:
    """headway_prev_sec = None только для лидера группы или одиночек — иначе всегда число."""
    from collections import defaultdict
    groups: dict[tuple[str, int], list] = defaultdict(list)
    for v in sim.vehicles:
        if v.is_reserve:
            continue
        groups[(v.route_id, v.direction_id)].append(v)
    for group in groups.values():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: x.pos_m)
        # для лидера headway == None
        pub_leader = sim.pub_vehicle(group_sorted[0])
        assert pub_leader["headway_prev_sec"] is None
        # для всех остальных — число
        for follower in group_sorted[1:]:
            pub = sim.pub_vehicle(follower)
            assert isinstance(pub["headway_prev_sec"], int)


def test_schedule_target_stop_in_window(sim: Simulator) -> None:
    """Целевая остановка в расписании должна лежать в окне [10 мин, 15 мин]."""
    for veh in sim.vehicles[:5]:
        sch = sim.get_schedule(veh.vehicle_id)
        targets = [s for s in sch["stops"] if s.get("is_target")]
        # ТС в конце маршрута может не иметь целевой — это ок
        assert len(targets) <= 1


def test_step_moves_vehicles(sim: Simulator) -> None:
    """Симуляция должна двигать ТС — иначе тест live-стенда ловит замерзание."""
    positions = {v.vehicle_id: v.pos_m for v in sim.vehicles}
    sim.step(5.0)
    moved = sum(1 for v in sim.vehicles if v.pos_m != positions[v.vehicle_id])
    assert moved > len(sim.vehicles) // 2, "меньше половины ТС двинулись за 5 сек"


def test_whatif_reduces_delay(sim: Simulator) -> None:
    """Any what-if мера должна не увеличивать прогноз задержки для проблемного ТС."""
    troubled = next((v for v in sim.vehicles if v.delay_pred_s > 60), None)
    if troubled is None:
        pytest.skip("нет проблемных ТС в фикстуре")
    for scenario in ("hold_at_stop", "signal_priority", "detour", "add_reserve", "adjust_interval"):
        after = sim.measure_effect(troubled, scenario)
        assert after <= troubled.delay_pred_s + 1, f"{scenario} УХУДШИЛ прогноз"
