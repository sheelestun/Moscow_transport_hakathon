"""Готовит для фронта JSON с почасовой статистикой опозданий.

Читает ``statistics/tables/baseline_metrics.csv`` (split=train, dimension=hour)
и пишет ``frontend/js/hourly-stats.js`` — эта же таблица лежит в бандле
дашборда как ``App.HOURLY_STATS``. Диспетчер видит, в какие часы модели
исторически труднее — это помогает интерпретировать текущие алерты.

Запуск при обновлении baseline_metrics.csv::

    python backend/scripts/generate_hourly.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "statistics" / "tables" / "baseline_metrics.csv"
OUT = ROOT / "frontend" / "js" / "hourly-stats.js"


def main() -> None:
    by_hour: dict[int, dict] = {}
    with SRC.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["split"] != "train" or row["dimension"] != "hour":
                continue
            h = int(row["group"])
            by_hour[h] = {
                "points": int(row["points"]),
                "mae": round(float(row["mae_cur_dev_s"]), 1),
                "mae_zero": round(float(row["mae_zero_s"]), 1),
            }
    for h in range(24):
        by_hour.setdefault(h, {"points": 0, "mae": None, "mae_zero": None})
    payload = {str(h): by_hour[h] for h in range(24)}
    js = (
        "// Автогенерируется из statistics/tables/baseline_metrics.csv\n"
        "// scripts/generate_hourly.py — среднее опоздание по часам суток (train).\n"
        "window.App = window.App || {};\n"
        f"window.App.HOURLY_STATS = {json.dumps(payload, ensure_ascii=False, indent=2)};\n"
    )
    OUT.write_text(js, encoding="utf-8")
    print(f"hourly: 24 часа → {OUT}")


if __name__ == "__main__":
    main()
