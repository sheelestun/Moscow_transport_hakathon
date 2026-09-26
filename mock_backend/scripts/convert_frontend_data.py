"""Конвертирует frontend/js/mock-routes.js и mock-signals.js в JSON для бэкенда.

Запускается один раз при обновлении маршрутов/светофоров Вероникой:

    python mock_backend/scripts/convert_frontend_data.py

На выходе — ``mock_backend/src/routes.json`` и ``mock_backend/src/signals.json``,
их читает :mod:`routes_data`. Тем самым один и тот же источник маршрутов
для мока и live-режима — фронт и бэк не разъезжаются.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend" / "js"
OUT_ROUTES = ROOT / "mock_backend" / "src" / "routes.json"
OUT_SIGNALS = ROOT / "mock_backend" / "src" / "signals.json"

# JS keys → нужно закавычить их для JSON.
JS_KEYS = ("route_id", "transport_type", "name", "loop", "dirs", "osm_name", "line", "stops")


def _js_array_to_json(src: str, var: str) -> str:
    # достаём тело правой части присваивания App.<var> = [ ... ];
    m = re.search(rf"App\.{re.escape(var)}\s*=\s*(\[.*?\]|\{{.*?\}})\s*;", src, flags=re.DOTALL)
    if not m:
        raise RuntimeError(f"не нашёл App.{var} в исходнике")
    body = m.group(1)
    # unquoted keys → JSON keys
    for k in JS_KEYS:
        body = re.sub(rf"\b{k}\s*:", f'"{k}":', body)
    # trailing запятые перед ] или }
    body = re.sub(r",(\s*[\]}])", r"\1", body)
    return body


def main() -> None:
    routes_src = (FRONT / "mock-routes.js").read_text(encoding="utf-8")
    signals_src = (FRONT / "mock-signals.js").read_text(encoding="utf-8")

    routes_json = _js_array_to_json(routes_src, "MOCK_ROUTES")
    signals_json = _js_array_to_json(signals_src, "MOCK_SIGNALS")

    # валидация — если JSON битый, вылетит здесь
    routes = json.loads(routes_json)
    signals = json.loads(signals_json)

    OUT_ROUTES.write_text(json.dumps(routes, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_SIGNALS.write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"routes:  {len(routes)} маршрутов → {OUT_ROUTES}")
    print(f"signals: {len(signals)} маршрутов → {OUT_SIGNALS}")


if __name__ == "__main__":
    main()
