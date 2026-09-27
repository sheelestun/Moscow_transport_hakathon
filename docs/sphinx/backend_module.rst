Backend
=======

Боевой бэкенд (пакет ``app`` в ``backend/``): принимает поток NDTP-телеметрии от терминалов
(и эмулятора), сопоставляет его с расписанием, считает производные признаки, получает прогнозы
ML-сервиса в горизонте 10–15 минут, поднимает алерты и отдаёт всё диспетчерскому дашборду.

Поток данных::

    терминалы / эмулятор ──TCP:9201──▶ ndtp.server ─┐
    traffic.csv (история) ──────────▶ ingest.replay ─┴─▶ ingest.pipeline ──▶ state.fleet
                                                               (unit → ТС, датасетные часы)
    state.fleet ──▶ predict.predictor ──HTTP──▶ ML-сервис
                          │
                          ├──▶ alerts ──▶ api.ws (WebSocket) ──▶ дашборд
                          └──▶ db.history (Postgres)

Спецификация HTTP API — Swagger UI бэкенда: ``https://api.mowtransit.ru/docs``
(локально ``http://localhost:8000/docs``). Подробности, замеры и инструкции — ``backend/README.md``.

Модули
------

.. autosummary::
   :toctree: _autosummary
   :recursive:

   app.main
   app.config
   app.clock
   app.ndtp.protocol
   app.ndtp.server
   app.ingest.models
   app.ingest.dataset
   app.ingest.pipeline
   app.ingest.replay
   app.state.schedule
   app.state.arrivals
   app.state.fleet
   app.predict.client
   app.predict.payload
   app.predict.predictor
   app.alerts
   app.db.history
   app.api.views
   app.api.dashboard
   app.api.ws
