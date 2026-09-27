# infra/

**Production deployment (VPS, mowtransit.ru): [`deploy/`](deploy/README.md)** — compose stack, nginx sites, runbook.

Инфраструктурные конфиги для локального стенда Moscow Transport Hackathon.

## emulator-config.json

Шаблон конфигурации для NDTP-эмулятора (`ndtp-telemetry-emulator:1.0`). Задаёт, куда слать NDTP-пакеты и какие единицы транспорта симулировать.

Применить (эмулятор поднят через `docker compose --profile emulator up -d`):

```bash
curl -X POST http://localhost:18080/api/config \
  -H 'Content-Type: application/json' \
  -d @infra/emulator-config.json
```

`targetHost=backend` — DNS-имя внутри docker-сети `msk`. При запуске эмулятора вне compose заменить на IP хоста, где слушает backend (например `192.168.1.10`).

### Схема units

- `unitId` (Long) — идентификатор единицы транспорта. Валидные значения брать из `dataset/train/traffic.csv` (колонка unit_id); здесь для примера указан один.
- `intervalMs` (Long) — период отправки пакетов, мс.
- `autoGenerate` (bool) — если true, эмулятор сам генерирует значения полей ячеек (движение по маршруту, скорость и т.д.); если false — шлёт ровно то, что в `cells.fields`.
- `cells` — массив NDTP-ячеек в пакете (тип + значения полей).

Полная схема — в `dataset/ndtp-telemetry-emulator.tar` (`TelemetryEmulatorConfig.class` после `docker load` + распаковки образа).
