# Локальный запуск — Moscow Transport Hackathon

Полный стек (ML + backend + frontend + NDTP-эмулятор + redis + postgres) поднимается одной командой в Docker Compose. Отдельно можно гонять только backend (для отладки NDTP-парсера, без ML).

## 0. Что нужно на машине

- Docker и Docker Compose v2 (`docker compose version` должен работать)
- Python 3.12+ и `pip` (для тестов и локального dev-loop без docker)
- Node.js 20+ (только если хочется править фронт без docker)
- ~4 ГБ RAM, ~2 ГБ диска

## 1. Клонировать репу

```bash
git clone git@github.com:sheelestun/Moscow_transport_hakaton.git
cd Moscow_transport_hakaton
git checkout integration/backend-e2e-demo   # ветка интеграции, здесь свежее чем в main
```

## 2. Забрать датасет

Датасет лежит на Яндекс.Диске (~200 МБ, в git не пушим): <https://disk.yandex.ru/d/CA6tsj4aJJ4Aaw>

Скачать и распаковать в `./dataset/` — должна получиться такая структура:

```
dataset/
├── docs/                            # PDF-описание NDTP-протокола
├── labels/                          # labels_{train,test,validate}.csv
├── train/  test/  validate/         # traffic.csv, schedule.csv
├── sample_submission.csv
├── ndtp-telemetry-emulator.tar      # OCI-образ Java-эмулятора (129 МБ)
└── README.md
```

Быстрый чек, что подмонтировали правильно:

```bash
ls dataset/validate/     # должен показать traffic.csv, schedule.csv
du -sh dataset/          # ~200 МБ
```

Если датасет лежит не в `./dataset` (например, symlink'и на внешний диск), путь можно переопределить: `DATASET_HOST_DIR=/abs/path/to/dataset docker compose ...` либо через `docker-compose.override.yml` (см. §11).

## 3. Один раз — загрузить образ эмулятора

Эмулятор поставляется OCI-архивом, в Docker Hub его нет:

```bash
docker load -i dataset/ndtp-telemetry-emulator.tar
docker image ls | grep ndtp-telemetry-emulator   # должен появиться :1.0
```

## 4. Что за два бэкенда

В репе живут два бэкенда, оба под compose:

- **`backend`** (сборка из `./mock_backend`) — стабильный mock-диспетчер с встроенным симулятором ТС. Поднимается всегда, порт `8000`. То, что жюри видело на демо.
- **`backend-dev`** (сборка из `./backend`) — новый боевой бэкенд: NDTP ingest → schedule matching → ML → dispatcher API + Postgres history. Спрятан за профилем `dev`, порт `8010` + TCP `9201` для NDTP.

## 5. Поднять весь стек

**Только mock (демо как раньше)**:

```bash
docker compose up -d --build
```

**Mock + новый бэкенд рядом** (mock на :8000, новый на :8010):

```bash
docker compose --profile dev up -d --build
```

**Только новый бэкенд с зависимостями** (без mock):

```bash
docker compose --profile dev up -d --build ml redis postgres backend-dev
```

**Фронт указать на новый бэкенд** (иначе SPA бьёт в mock):

```bash
BACKEND_PORT=8010 docker compose --profile dev up -d
```

Проверить состояние: `docker compose ps` (ждём пока healthy, 10-20 сек).

- **Дашборд**: <http://localhost:3000>
- **Backend mock Swagger**: <http://localhost:8000/docs>
- **Backend-dev Swagger**: <http://localhost:8010/docs>
- **ML Swagger**: <http://localhost:8001/docs>
- **Эмулятор API**: <http://localhost:18080> (POST `/api/config` — конфиг из `infra/emulator-config.json`)

Логи одного сервиса: `docker compose logs -f backend-dev`. Остановить: `docker compose --profile dev down` (persistent postgres не удаляется).

## 6. Проверить, что работает

Открыть <http://localhost:3000>, на карте должны быть машины и алерты в правой панели.

Для нового бэкенда (`backend-dev`) быстрый чек:

```bash
curl -s http://localhost:8010/health | python3 -m json.tool | head -40
```

Смотрим:
- `ndtp.listening: true`, `ndtp.port: 9201` — TCP-сервер поднят
- `replay.status: running` — реплей traffic.csv идёт
- `predictor.ml_available: true`, `predictor.horizon_ok_share: 1.0` — прогнозы ML в горизонте
- `history.connected: true` — Postgres пишется
- `alerts.active: N` — алерты

Полный сценарий демо (8 шагов) — [`infra/DEMO.md`](./infra/DEMO.md).

## 7. Пустить NDTP-эмулятор в новый бэкенд

`backend-dev` слушает NDTP на TCP `9201`. Эмулятор указываем на него через HTTP API:

```bash
curl -X POST http://localhost:18080/api/config \
  -H "Content-Type: application/json" \
  -d '{"targetHost":"backend-dev","targetPort":9201,"autoGenerate":true}'
```

`autoGenerate` — случайный walk (для проверки, что парсер и CRC живые). Живой поток с реальными траекториями из `validate/traffic.csv`:

```bash
python infra/emulator_replay.py --dataset ./dataset \
  --emu-url http://localhost:18080 \
  --target-host backend-dev --target-port 9201 \
  --clock-url http://localhost:8010
```

`--clock-url` синхронизирует эмулятор с датасетными часами бэкенда. Через `/health` увидим `ndtp.fixes_total` растущим и `ingest.vehicles_live_ndtp > 0`.

## 8. Автотесты (без docker)

Быстрые unit/smoke-тесты бэкендов и ML — гоняются в CI и локально:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r mock_backend/requirements.txt \
            -r ml/requirements.txt pytest httpx pydantic-settings asyncpg
pytest                # backend/tests + mock_backend/tests + ml/tests + statistics/tests
```

Тесты `backend/tests/test_history.py` работают только если задан `TEST_DATABASE_URL` (иначе skipped).

## 9. Только backend-dev локально (для отладки NDTP-парсера)

Без docker, без ML:

```bash
cd backend
pip install -r requirements.txt
DATASET_DIR=../dataset ML_URL=http://127.0.0.1:1 \
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

`ML_URL` бьётся в пустоту → бэкенд деградирует до fallback-прогнозов, но всё остальное (NDTP TCP на 9201, `/vehicles`, `/state`, `/alerts`, `/ws`) работает.

Отдельно только NDTP-листенер (для нагрузочного тестирования парсера):

```bash
cd backend
python -m app.ndtp                          # слушает TCP :9201, логирует каждый fix
python scripts/ndtp_loadtest.py --help      # синтетический нагруз
```

## 10. Только ML (переобучение)

```bash
cd ml
pip install -r requirements.txt
# оценка (~5 мин, пишет ml/artifacts/catboost_metrics.json)
python src/train_catboost.py --dataset ../dataset --eval
# финальная модель на train+test + сабмит на validate (~3 мин)
python src/train_catboost.py --dataset ../dataset --fit --out submission.csv
```

Артефакты (`catboost_seed*.cbm`, `catboost_meta.json`) читаются live-контейнером ML при старте — если поменяли, дёрни `curl -X POST http://localhost:8001/reload`.

## 11. Локальный override (`docker-compose.override.yml`)

Compose автоматически подхватывает `docker-compose.override.yml` рядом с базовым файлом. Файл в git не коммитим — там локальные особенности машины. Пример (dataset вне репы, локальный postgres на 5432 уже занят):

```yaml
services:
  backend-dev:
    volumes:
      - /abs/path/to/dataset:/data:ro
    environment:
      # asyncpg по умолчанию ssl='prefer', postgres:16-alpine SSL не даёт.
      DATABASE_URL: postgresql://msk:msk@postgres:5432/msk_transport?sslmode=disable
      # Датасетные часы фиксируем на утро 2026-01-06 — иначе после полуночи МСК clock уезжает
      # на 2026-01-07, где данных нет. speed=5 → 8 часов датасета за 1.5 часа реального времени.
      CLOCK_START: "2026-01-06T08:00:00"
      CLOCK_SPEED: "5"
  postgres:
    # !reset — compose по умолчанию мержит списки портов, а не заменяет;
    # без reset базовый "5432:5432" остаётся и коллидит с системным postgres.
    ports: !reset []
```

## 12. Порты и что на каких сидит

| Сервис | Хост-порт | Внутри compose | Наружу через |
|---|---|---|---|
| frontend | 3000 | nginx :80 | Браузер |
| backend (mock) | 8000 | uvicorn :8000 | Браузер / фронт |
| backend-dev | 8010 (HTTP), 9201 (NDTP TCP) | :8000, :9201 | Браузер / эмулятор |
| ml | 8001 | uvicorn :8001 | Backend, curl |
| ndtp-emu | 18080 (HTTP) | :18080 | Кнопка/скрипт |
| postgres | 5432 (по умолчанию) | :5432 | psql |
| redis | — | :6379 | только внутри сети |

## 13. Типовые проблемы

- **`ndtp-telemetry-emulator:1.0 not found`** → забыт `docker load -i` из шага 3.
- **`ML healthcheck failed`** → в `ml/artifacts/` нет `catboost_seed*.cbm`; обучить (шаг 10) или скачать артефакты из ветки `ml/catboost-tabular`.
- **`backend-dev` в `history.connected: false`** → postgres в другой compose-сети или порт 5432 занят системным postgres; см. override в §11 (`ports: !reset []`).
- **`address already in use :5432`** → у системы свой postgres на 5432; override с `ports: !reset []` полностью убирает публикацию наружу — компонентам достаточно доступа внутри сети `msk`.
- **`postgres` резолвится в 198.18.x.x** → VPN-клиент (FlClashX/Clash) перехватывает DNS; проверить, что контейнер postgres реально в сети `moscow_transport_hakaton_msk` (`docker network inspect moscow_transport_hakaton_msk`). Если нет — `docker compose --profile dev up -d --force-recreate postgres`.
- **Дашборд пустой, `status: degraded`** → бэкенд не поднялся или не видит датасет; `docker compose logs backend-dev`.
- **`No module named 'catboost'` в pytest** → `pip install -r ml/requirements.txt` (для юнит-тестов ML нужен catboost).
- **`Address already in use :8000`** → у тебя уже что-то на 8000; `docker compose down` или поднять только новый бэкенд на 8010.
- **NDTP не пишет fix'ы** → эмулятор шлёт на mock-бэкенд (`targetHost: "backend"`), а не на `backend-dev`. POST-нуть новый конфиг (§7).
- **Ночью бэкенд молчит** → `DatasetClock` по умолчанию берёт текущее время суток МСК на 2026-01-06; после 00:00 МСК он уезжает на 2026-01-07, где данных нет, и `replay.status: finished`. Фикс — задать `CLOCK_START` в override (см. §11): `CLOCK_START=2026-01-06T08:00:00`, при желании `CLOCK_SPEED=5` — 8 часов датасета проиграются за 1.5 часа.

## Что где искать

- `ARCHITECTURE_AND_ROLES.md` — как компоненты соединены, кто что делает
- `backend/README.md` — детали нового боевого бэкенда (NDTP, state, predictor, history)
- `ml/README.md` — детали ML-трека (какие фичи, чем валидируется, ONNX)
- `infra/DEMO.md` — сценарий показа жюри
- `statistics/REPORT.md` — анализ датасета Шелестова с графиками
