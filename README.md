# Предиктор задержек наземного транспорта Москвы

Система заранее предупреждает диспетчера об опоздании: **за 10–15 минут до остановки**, с причиной и
рекомендацией. Она слушает NDTP-телеметрию автобусов, сопоставляет каждый пакет с расписанием и прогнозирует
задержку CatBoost-ансамблем. Проект сделан для хакатона Московского транспорта (2026).

| | |
|---|---|
| Дашборд диспетчера | https://app.mowtransit.ru |
| **Инструкция для жюри** | https://docs.mowtransit.ru/getting_started.html |
| Документация по коду (Sphinx) | https://docs.mowtransit.ru |
| OpenAPI / Swagger backend | https://api.mowtransit.ru/docs |
| Состояние системы и живые метрики | https://api.mowtransit.ru/health |

## Как устроено

```
NDTP-эмулятор / терминалы ──┐
                            ├──► Backend ──► ML-ядро ──► Backend ──► Дашборд
исторический traffic.csv ───┘
```

| Модуль | Папка | Что делает |
|---|---|---|
| **ML-ядро** | [`ml/`](ml/README.md) | FastAPI-сервис на CatBoost-ансамбле: задержка на целевой остановке, интервал, вероятность опоздания, причина (SHAP) и рекомендация. Обучение, валидация, экспорт в ONNX. |
| **Backend** | [`backend/`](backend/README.md) | Приём и разбор NDTP по TCP, сопоставление с расписанием, прибытия по GPS, производные признаки. Выбирает целевую остановку в окне (T+10, T+15] мин, вызывает ML, формирует и проверяет алерты. REST + WebSocket, история в Postgres. |
| **Дашборд** | [`frontend/`](frontend/README.md) | Карта с ТС и цветом риска, лента алертов, карточка инцидента, «Что если…», перемотка. Статика под nginx. |

Модули общаются только по HTTP и работают независимо. Без ML backend прогнозирует по текущему отклонению,
без Postgres копит историю в памяти, без NDTP проигрывает исторический датасет.

## Запуск

Нужны Docker с Compose v2 и около 4 ГБ RAM. Модели лежат в репозитории (`ml/artifacts`), датасет нужно скачать у
организаторов: https://disk.yandex.ru/d/CA6tsj4aJJ4Aaw.

```bash
git clone https://github.com/sheelestun/Moscow_transport_hakathon.git
cd Moscow_transport_hakathon
# распакуйте архив датасета в ./dataset, чтобы появился файл dataset/validate/traffic.csv
docker compose up -d --build
```

| | |
|---|---|
| Дашборд | http://localhost:3000 |
| Swagger backend | http://localhost:8000/docs |
| Swagger ML-ядра | http://localhost:8001/docs |
| Приём NDTP (TCP) | `localhost:9201` |

По умолчанию backend проигрывает исторический `validate/traffic.csv`, и датасетные часы стартуют с 08:00
2026-01-06. Если нужен другой момент, задайте `CLOCK_START`. Поток через эмулятор NDTP организаторов:

```bash
docker load -i dataset/ndtp-telemetry-emulator.tar
docker compose --profile emulator up -d
pip install httpx numpy pandas
python infra/emulator_replay.py --dataset ./dataset --emu-url http://localhost:18080 \
    --target-host backend --target-port 9201 --clock-url http://localhost:8000
```

Подробно о том, как подать поток, где смотреть прогнозы, алерты и метрики и как проверить деградацию, написано в
[инструкции для жюри](https://docs.mowtransit.ru/getting_started.html). Развёртывание на сервере (nginx, TLS)
описано в [`infra/deploy/`](infra/deploy/README.md).

## Результаты

- **Точность**: MAE 39.8 с на holdout (бейзлайн 93.4 с), скор на платформе ≈ 1.0.
- **Горизонт**: у 100% прогнозов на живом NDTP-потоке целевая остановка попадает в окно (T+10, T+15] мин.
- **Скорость**: ML отвечает за 10–30 мс на ТС; цикл backend → ML → backend для всей пачки ТС занимает p95 370 мс
  на сервере.
- **NDTP**: 8 000 терминалов с отправкой раз в секунду обрабатываются без потерь (p99 1.2 мс) на одном ядре.

Все замеры с командами для воспроизведения собраны в [`ml/PERFORMANCE.md`](ml/PERFORMANCE.md) и
[`backend/README.md`](backend/README.md).

## Сабмит и тесты

```bash
python ml/src/train_catboost.py --dataset ./dataset --fit --out submission.csv    # обучение + submission.csv
python ml/src/verify_streaming.py --dataset ./dataset --submission submission.csv  # онлайн-инференс == батч

pip install -r backend/requirements.txt -r ml/requirements.txt pytest httpx
pytest                                                                             # backend + ml + statistics
```

CI запускает `pytest` на каждый пуш ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

## Документация

- https://docs.mowtransit.ru — Sphinx: инструкция для жюри, ML, API ML-ядра, backend, справочник по коду.
  Собрать локально: `pip install sphinx sphinx-rtd-theme && make -C docs/sphinx html`.
- [`ml/README.md`](ml/README.md) — модель, признаки, обучение; [`ml/EMULATOR.md`](ml/EMULATOR.md) — эмулятор NDTP.
- [`backend/README.md`](backend/README.md) — устройство backend, признаки, алерты, нагрузочный тест NDTP.
- [`statistics/REPORT.md`](statistics/REPORT.md) — анализ датасета.

## Команда

| Роль | Кто |
|---|---|
| ML | Степан, Фёдор |
| Backend | Даниил Герман |
| Frontend | Вероника |
| Данные и аналитика | Даниил Шелестов |

Стек: Python 3.12, CatBoost, FastAPI, PostgreSQL, nginx, MapLibre, Docker.
