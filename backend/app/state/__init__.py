"""Vehicle state: the planned schedule, per-vehicle telemetry buffers, and what's derived from them —
arrivals at stops, current deviation from the schedule, segment speed, dwell time."""

from .arrivals import Arrival, detect_arrivals
from .fleet import Derived, Fleet, VehicleState
from .schedule import StopVisit, VehicleSchedule, load_schedule

__all__ = ["Arrival", "Derived", "Fleet", "StopVisit", "VehicleSchedule", "VehicleState", "detect_arrivals",
           "load_schedule"]
