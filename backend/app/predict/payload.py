"""Building ML ``PredictRequest`` bodies from vehicle state (contract: ``ml/src/inference_service.py``)."""

from __future__ import annotations

from datetime import datetime

from ..ingest import Ping
from ..state import StopVisit, VehicleSchedule

# Fixed formats: datetime.isoformat() drops ".ffffff" when microseconds are 0, which mixes two formats
# in one field. The ML side happens to parse both; the contract shouldn't rely on that.
TS = "%Y-%m-%dT%H:%M:%S.%f"


def sample_id(tr_id: int, T: datetime) -> str:
    """``{tr_id}_{unix(T)}`` — the dataset's convention, so live predictions can be diffed against the batch."""
    return f"{tr_id}_{int((T - datetime(1970, 1, 1)).total_seconds())}"


def telemetry_rows(pings: list[Ping]) -> list[dict]:
    return [{"tr_id": p.tr_id, "event_time": p.event_time.strftime(TS), "lon": p.lon, "lat": p.lat,
             "speed": p.speed_kmh, "location_valid": p.location_valid, "is_hist_data": int(p.is_hist)} for p in pings]


def schedule_rows(s: VehicleSchedule) -> list[dict]:
    """The vehicle's whole day plan: the model derives trip structure (position in trip, terminals ahead) from
    the rows it gets, so a truncated plan would silently corrupt those features."""
    return [{"tr_id": s.tr_id, "tt_action_item_id": v.stop_id, "time_begin": v.plan.strftime(TS), "geom": v.geom,
             "manual_fill": v.manual_fill, "building_address": v.name} for v in s.visits]


def predict_request(tr_id: int, T: datetime, target: StopVisit, cur_dev_s: float, pings: list[Ping],
                    schedule: list[dict]) -> dict:
    return {
        "sample_id": sample_id(tr_id, T),
        "tr_id": tr_id,
        "T": T.strftime(TS),
        "target_stop_id": target.stop_id,
        "target_time_begin": target.plan.strftime(TS),
        "cur_dev_s": cur_dev_s,
        "telemetry": telemetry_rows(pings),
        "schedule": schedule,
    }
