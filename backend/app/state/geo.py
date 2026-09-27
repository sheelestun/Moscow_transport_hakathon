"""Distances and time conversions shared by the state modules (same formulas as the ML module)."""

from __future__ import annotations

from datetime import datetime

import numpy as np

_EPOCH = datetime(1970, 1, 1)


def dist_m(lon1, lat1, lon2, lat2):
    """Equirectangular distance in metres — identical to ``ml/src/features/tabular.dist_m``."""
    k = np.pi / 180
    return 6371000.0 * np.hypot((np.asarray(lon1) - lon2) * k * np.cos(lat2 * k), (np.asarray(lat1) - lat2) * k)


def epoch_s(t: datetime) -> float:
    """Seconds since the epoch of a naive dataset timestamp, read as UTC — as numpy's datetime64 does in ML."""
    return (t - _EPOCH).total_seconds()
