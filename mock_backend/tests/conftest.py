"""pytest-конфигурация для backend-тестов: путь к исходникам, изолированные фикстуры.

Тесты гоняются локально и в CI без Docker: симулятор в процессе, ML/сеть не нужны
(HTTP к ML мокается фолбэком, см. `_ml_get` в `main.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))
