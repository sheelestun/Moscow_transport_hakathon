# Frontend — диспетчерский BI-дашборд

Карта Москвы с ТС и светофором риска, лента алертов, карточка инцидента, панель What-if.

Стек: чистые HTML/CSS/JavaScript + [MapLibre GL 5](https://maplibre.org/) (векторная карта). Подложка — [OpenFreeMap](https://openfreemap.org/) (свежий OpenStreetMap, без ключа), стиль свой: только дороги, вода, парки. **Сборка и npm не нужны.**

## Как запустить

```bash
cd frontend
python -m http.server 3000
```
Открыть http://localhost:3000

Двойной клик по `index.html` тоже обычно работает, но через `http.server` надёжнее.

### Режимы
| Адрес | Что показывает |
|---|---|
| `http://localhost:3000` | демо-данные (мок), бэкенд не нужен |
| `http://localhost:3000/?mode=live` | живой бэкенд на `http://localhost:8000` |
| `?mode=live&api=http://HOST:8000&ws=ws://HOST:8000/ws` | бэкенд на другом адресе |

### Docker
```bash
docker build -t dashboard ./frontend
docker run --rm -p 3000:80 dashboard
```

## Структура
```
frontend/
├── index.html        разметка страницы
├── css/styles.css    внешний вид (цвета — в :root сверху)
└── js/
    ├── config.js       настройки: режим, адрес API, пороги риска
    ├── labels.js       русские подписи для кодов причин/рекомендаций, форматирование
    ├── geo.js          геометрия: расстояния, проекция остановок на линию маршрута
    ├── mock-routes.js  настоящие маршруты Мосгортранса из OpenStreetMap: м1, м2, м5, 196 и кольцевой Б (трассы и остановки)
    ├── mock-signals.js реальные светофоры на демо-маршрутах (OpenStreetMap); фазы в демо — симуляция
    ├── mock.js         фейковый бэкенд: движение ТС, расписания, опоздания, алерты
    ├── api.js          живой бэкенд: REST + WebSocket с переподключением
    ├── basemap.js      стиль подложки карты (что рисовать и какими цветами)
    ├── map.js          карта MapLibre: маршруты, ТС, подсветка выбранного маршрута
    ├── sidebar.js      правая панель: алерты, маршруты, карточка ТС, расписание
    ├── whatif.js       окно «Что если…»: сравнение всех мер и кнопка «Применить» (в демо)
    └── app.js          состояние и связка всего вместе
```

## Что ждём от бэкенда (контракт)

Полная спецификация — Swagger backend (`/docs`, https://api.mowtransit.ru/docs). Главное для фронта:

**`GET /routes`**
```json
[{ "route_id": "М1", "name": "Охотный Ряд — Сокол", "transport_type": "bus",
   "geometry": [[55.7577, 37.6146], [55.7654, 37.6040]],
   "stops": [{ "stop_id": "53700172828", "name": "Пушкинская пл.", "lat": 55.7654, "lon": 37.6040 }] }]
```

`directions` (желательно): оба направления маршрута, каждое по своей стороне дороги —
`[{ "direction_id": 0, "name": "Охотный Ряд → Сокол", "geometry": [[lat, lon], ...], "stops": [...] }, { "direction_id": 1, ... }]`.
Тогда у ТС нужно поле `direction_id`, и подсветка маршрута рисуется по нужной стороне. Без `directions` используется `geometry`.

`transport_type` (необязательно): `bus` | `electrobus` | `trolleybus` | `tram`. Если приходит — в меню «Линии» появляется выбор по видам транспорта.

**`GET /vehicles`**, WS `vehicle.update` (одно ТС или `{"type":"vehicle.update","vehicles":[...]}`)
```json
{ "vehicle_id": "131672", "route_id": "М1", "lat": 55.77, "lon": 37.59, "speed": 18,
  "delay_now_sec": 80, "delay_pred_sec": 187, "risk_score": 0.83, "updated_at": "2026-01-06T03:35:00Z" }
```

**`GET /alerts?active=true`**, WS `alert.new` — как в 7.2, плюс желательно:
`target_stop_name`, `confidence`, `top_features`, `model_version`.
WS `alert.resolved`: `{ "type": "alert.resolved", "alert_id": "a-91021" }`.

**`POST /whatif`** `{ "scenario": "add_reserve" | "adjust_interval" | "detour" | "signal_priority" | "hold_at_stop" (+ желательно "short_turn" | "express"), "route_id", "vehicle_id", "at_stop_id" }` (коды — как в `WHATIF_DELTA_MAP` ML-сервиса; бэкенд может звать `POST /whatif/predict` у ML для каждого ТС маршрута) →
```json
{ "summary": { "avg_delay_before_sec": 140, "avg_delay_after_sec": 60, "red_before": 2, "red_after": 0 },
  "vehicles": [{ "vehicle_id": "131672", "delay_before_sec": 187, "delay_after_sec": 70,
                 "risk_before": 0.83, "risk_after": 0.2 }] }
```

**`GET /vehicles/{vehicle_id}/schedule`** — расписание текущего рейса (для таблицы и подсветки маршрута)
```json
{ "vehicle_id": "131672", "route_id": "М1", "direction": "Сокол → Охотный Ряд",
  "stops": [
    { "stop_id": "53700172828", "name": "Динамо", "lat": 55.789, "lon": 37.558,
      "time_plan": "2026-01-06T03:20:00Z", "time_fact": "2026-01-06T03:21:50Z", "delay_sec": 110, "status": "passed" },
    { "stop_id": "53700172829", "name": "Белорусский вокзал", "lat": 55.776, "lon": 37.582,
      "time_plan": "2026-01-06T03:27:00Z", "time_pred": "2026-01-06T03:29:48Z", "delay_sec": 168, "status": "next" },
    { "...": "...", "status": "upcoming", "is_target": true }
  ] }
```
`status`: `passed` (есть `time_fact`) | `next` | `upcoming` (есть `time_pred`). `is_target` — остановка из окна T+10…15 мин.
В `schedule.csv` это `time_begin` (план) и `time_fact_begin` (факт).

В `vehicle.update` для жёлтых/красных ТС желательно также: `reason_pattern`, `recommendation`, `top_features`, `confidence` — чтобы по клику на любое ТС было видно, почему оно опаздывает.

**`GET /metrics/model`** → как у ML-сервиса: `{ "mae_test_s": 43.7, "latency_ms_p50": 18, "model_version": "..." }` (бэкенд может просто проксировать)

Светофор: `risk_score ≥ 0.7` — красный, `≥ 0.35` — жёлтый, иначе зелёный (`js/config.js`).
Бэкенду нужно разрешить CORS для адреса дашборда.

## Светофоры (демо)

Места светофоров — реальные, из OpenStreetMap (`highway=traffic_signals`), по 3–26 перекрёстков на маршрут.
Фазы красный/зелёный **симулируются** (цикл 80–100 с): реальные фазы есть у ЦОДД, при внедрении их надо брать оттуда.
В демо автобус останавливается на красном, в карточке видно «стоит на красном 25 с».
Мера «Приоритет на светофорах» — **условный** приоритет, как в Нидерландах (iVRI/KAR): 20 минут светофоры маршрута
продлевают зелёный (до 10 с) или включают его раньше (до 15 с), но только автобусу, который опаздывает больше
чем на минуту. Фазы для остальных не меняются. Светофор, который сейчас пропускает автобус, на карте с «П».

## Меры в «Что если…»

Как у диспетчеров в Европе и США:

| Мера | К кому | Когда имеет смысл |
|---|---|---|
| Приоритет на светофорах | опаздывающие ТС маршрута | опоздание > 1 мин |
| Придержать догоняющий | ТС, которое едет следом | интервал меньше 80% планового («паровозик»); не дольше 3 мин |
| Объезд затора | выбранное ТС | только если впереди затор |
| Развернуть раньше конечной | выбранное ТС | опоздание > 6 мин, не на кольцевых |
| Пустить экспрессом | выбранное ТС | опоздание > 3 мин; пропускает 4 остановки |
| Выпустить резерв / выровнять интервалы | весь маршрут | — |

`short_turn` и `express` пока считает только демо: бэкенд (`POST /whatif`, `WHATIF_DELTA_MAP` в ML) их не знает,
поэтому в live-режиме окно сравнения их не показывает (`scenarios` в `js/api.js`).
В живом режиме слой светофоров не показывается, пока бэкенд не отдаёт фазы (`waiting_signal` у ТС — необязательное поле).

## Смотреть на данных с сайта

Чтобы проверять свои правки на тех же данных, что на app.mowtransit.ru, не пуша их (нужен Docker):

```
frontend\dev\live.bat
```

и открыть http://localhost:3001/?mode=live&api=/api&ws=ws://localhost:3001/api/ws

Файлы берутся прямо из папки `frontend` — сохранили файл, обновили страницу (Ctrl+Shift+R). Запросы к API идут
через локальный nginx (`dev/nginx-live.conf`) на https://api.mowtransit.ru, поэтому браузер их не блокирует
(напрямую с localhost сайт запросы не принимает — CORS разрешён только для app.mowtransit.ru).
Без Docker: `python -m http.server 3000` в папке `frontend` — но тогда только демо-данные.

## Правки сразу на localhost:3000

Обычно контейнер `frontend` копирует файлы при сборке, и после правки нужен `docker compose up -d --build frontend`.
Чтобы видеть правки сразу, положите в корень репозитория `docker-compose.override.yml` (в git он не попадает):

```yaml
services:
  frontend:
    volumes:
      - ./frontend:/usr/share/nginx/html:ro
```

и один раз пересоберите: `docker compose up -d --build frontend`. Дальше — сохранили файл, обновили страницу.
`config.js` при этом не меняется: адрес бэкенда контейнер подставляет в отдельную копию (см. `entrypoint.sh`).
