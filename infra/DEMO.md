# Инструкция для жюри: как проверить систему

Система: NDTP-поток → backend (приём и разбор NDTP, сопоставление с расписанием, прибытия по GPS, выбор целевой
остановки через 10–15 мин, алерты) → ML-сервис (CatBoost: задержка, вероятность, интервал, причина) → дашборд.

## 1. Где смотреть (развёрнуто на сервере)

| Что | Адрес |
|---|---|
| Дашборд диспетчера | https://app.mowtransit.ru |
| API backend + Swagger | https://api.mowtransit.ru/docs |
| Документация (код, ML, backend) | https://docs.mowtransit.ru |
| NDTP-порт для терминалов / эмулятора | `ndtp.mowtransit.ru:9201` (TCP) |

ML-сервис наружу не опубликован — его вызывает только backend; его состояние видно в `/health` backend'а.

## 2. Проверка за 5 минут

1. **Дашборд** https://app.mowtransit.ru: карта, ТС с цветом риска (зелёный / жёлтый / красный), клик по ТС —
   карточка: прогноз опоздания на остановке через 10–15 мин, интервал, причина, рекомендация.
2. **Здоровье системы** — `GET https://api.mowtransit.ru/health`:
   - `ndtp.units_connected`, `ndtp.fixes_total`, `fixes_dropped` — живой NDTP-поток и потери;
   - `predictor.ml_available: true`, `predictions_ml`, `predictions_fallback` — прогнозы от ML;
   - **`predictor.horizon_ok_share`** — доля прогнозов, у которых целевая остановка в окне (T+10, T+15] мин
     (критерий 2; на сервере 27.09 — 1.0 на 700+ прогнозах);
   - `alerts.raised / verified / precision / mae_verified_s` — алерты и их сверка с фактическим прибытием
     (алерт выдаётся до события и потом проверяется фактом).
3. **Прогнозы** — `GET /predictions`: у каждого ТС `T`, целевая остановка, `lead_s`, `horizon_ok`,
   `delay_pred_s`, `risk_level`, `causes`, `recommendation_text`.
4. **Алерты** — `GET /alerts?active=true`; прибытия по GPS — `GET /state/vehicles/{tr_id}/arrivals`.
5. **Метрики модели** — `GET /metrics/model` (MAE по схемам валидации, живая latency, покрытие интервала).

Часы демо: датасет — один день (2026-01-06); backend проигрывает его синхронно с текущим московским временем
суток (`GET /clock`).

## 3. Подать свой NDTP-поток через эмулятор организаторов

Эмулятор в режиме `autoGenerate` шлёт случайное блуждание со случайными `unitId`, которых нет в датасете: backend
их примет и разберёт (растут `ndtp.fixes_total` и `ingest.unknown_units`), но сопоставить с расписанием не сможет.
Чтобы увидеть содержательный поток, мы проигрываем через эмулятор **настоящие треки 13 ТС** из датасета:

```bash
docker load -i dataset/ndtp-telemetry-emulator.tar
docker run -d --rm -p 18080:18080 --name ndtp-emu ndtp-telemetry-emulator:1.0
python infra/emulator_replay.py --dataset ./dataset --emu-url http://localhost:18080 \
    --target-host ndtp.mowtransit.ru --target-port 9201 --clock-url https://api.mowtransit.ru
```

Через минуту в `GET /ingest/vehicles` эти ТС идут с источником NDTP. Когда поток останавливается, через 60 с backend
сам переключает их на проигрывание `traffic.csv` — сервис не падает. Особенности эмулятора — `ml/EMULATOR.md`.

## 4. Проверка надёжности

- **ML недоступен** → backend выдаёт прогноз по текущему отклонению (`source: fallback`), дашборд работает,
  после возврата ML — снова прогнозы модели.
- **Обрыв NDTP** → переход на проигрывание датасета, восстановление после реконнекта.
- **Плохие данные** в ML (нет координат, ТС далеко от маршрута, пакеты позже T) → ответ со статусом
  `no_telemetry` / `off_route`, пакеты из будущего отбрасываются; замеры — `ml/PERFORMANCE.md`.

## 5. Запуск у себя

Полный стек (ML + backend + дашборд + Postgres) — тот же compose, что на сервере:

```bash
# из корня репозитория; dataset/ — распакованный архив организаторов, ml/artifacts/ — модели (train_catboost.py --fit)
cd infra/deploy
cp .env.example .env
# в .env для локального запуска:
#   POSTGRES_PASSWORD=<любой>
#   PUBLIC_API_BASE=http://localhost:8000
#   PUBLIC_WS_URL=ws://localhost:8000/ws
#   CORS_ORIGINS=["http://localhost:3000"]
#   CLOCK_START=2026-01-06T08:00:00        # утренний час пик
docker compose up -d --build
```

Дашборд — http://localhost:3000, API — http://localhost:8000/docs, NDTP — `localhost:9201`. Подробности деплоя —
`infra/deploy/README.md`, backend — `backend/README.md`, ML — `ml/README.md`.

> Корневой `docker-compose.yml` по умолчанию поднимает `mock_backend` — ранний симулятор без NDTP (оставлен для
> разработки фронта). Для проверки настоящей системы используйте `infra/deploy/docker-compose.yml`.

## 6. Что где описано

| Документ | О чём |
|---|---|
| `ml/README.md`, `ml/PERFORMANCE.md` | модель, признаки, валидация, скорость и надёжность ML |
| `ml/EMULATOR.md` | эмулятор NDTP, проигрывание настоящих треков, поведение ML на потоке |
| `ml/DATASET_STATS.md` | статистика датасета и ключевые находки |
| `backend/README.md`, `infra/deploy/README.md` | backend и развёртывание |
