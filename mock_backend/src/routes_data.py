"""Источник маршрутов и светофоров для симулятора и API.

Данные тянутся из JSON, сгенерированных из ``frontend/js/mock-routes.js`` и
``mock-signals.js`` (см. ``backend/scripts/convert_frontend_data.py``). Такой
единый источник исключает рассинхрон между мок-фронтом и live-бэкендом:
что видит Вероника — то же отдаёт live-режим.

Файлы:

- ``routes.json``  — 5 маршрутов (м1, м2, м5, 196, Б), у каждого 1..2
  направления с реальной трассой OSM и остановками по порядку.
- ``signals.json`` — светофоры (highway=traffic_signals) на маршрутах,
  сгруппированные по перекрёсткам (узлы ближе 45 м = один перекрёсток).
"""

from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent

with (_HERE / "routes.json").open("r", encoding="utf-8") as _fh:
    ROUTES: list[dict] = json.load(_fh)

with (_HERE / "signals.json").open("r", encoding="utf-8") as _fh:
    SIGNALS: dict[str, list[list[float]]] = json.load(_fh)


__all__ = ["ROUTES", "SIGNALS"]
