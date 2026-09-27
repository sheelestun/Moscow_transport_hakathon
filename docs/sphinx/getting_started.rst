Инструкция для жюри
===================

Как подать поток телеметрии, где увидеть прогнозы и алерты, как открыть дашборд и метрики.

Схема системы::

    NDTP-эмулятор / терминалы ──┐
                                ├──► Backend ──► ML-ядро ──► Backend ──► Дашборд
    исторический traffic.csv ───┘

Backend принимает NDTP, сопоставляет каждый пакет с расписанием, находит прибытия на остановки по GPS
и считает производные признаки: текущее отклонение, скорость на сегменте, время простоя. Каждые 5 секунд
он выбирает для каждого ТС целевую остановку в окне **(T+10, T+15] мин** и запрашивает прогноз у ML-ядра.
Красные прогнозы превращаются в алерты. Когда ТС доезжает до остановки, алерт сверяется с фактом.


1. Посмотреть без установки
---------------------------

Система развёрнута на сервере и работает на живом NDTP-потоке.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Что
     - Адрес
   * - Дашборд диспетчера
     - https://app.mowtransit.ru
   * - Swagger backend (OpenAPI)
     - https://api.mowtransit.ru/docs
   * - Состояние системы и метрики
     - https://api.mowtransit.ru/health
   * - Документация по коду (этот сайт)
     - https://docs.mowtransit.ru
   * - Приём NDTP (TCP)
     - ``ndtp.mowtransit.ru:9201``
   * - Исходный код
     - https://github.com/sheelestun/Moscow_transport_hakathon

ML-ядро наружу не опубликовано: его вызывает только backend. Состояние ML видно в ``/health`` backend'а,
а Swagger ML открывается при локальном запуске (раздел 2).

**Проверка за 5 минут:**

1. Откройте https://app.mowtransit.ru. На карте ТС окрашены по риску (зелёный / жёлтый / красный), справа лента
   алертов. Клик по ТС открывает карточку: прогноз опоздания на остановке через 10–15 минут, интервал, причина,
   участок маршрута, рекомендация и кнопка «Что если…».
2. Откройте https://api.mowtransit.ru/health (поля описаны в разделе 5).
3. В Swagger https://api.mowtransit.ru/docs выполните ``GET /predictions`` и ``GET /alerts``: это прогнозы и
   алерты, которые видит дашборд.

.. note::

   Датасет покрывает один день (2026-01-06). На сервере датасетные часы идут синхронно с текущим московским
   временем суток (``GET /clock``), поэтому ночью ТС на линиях мало. Если нужно гарантированно увидеть
   движение, запустите систему у себя: локально часы по умолчанию стартуют с 08:00.


2. Запуск у себя в Docker
-------------------------

Нужны Docker с Compose v2, около 4 ГБ RAM и датасет организаторов
(https://disk.yandex.ru/d/CA6tsj4aJJ4Aaw). Модели уже лежат в репозитории (``ml/artifacts``).

.. code-block:: bash

   git clone https://github.com/sheelestun/Moscow_transport_hakathon.git
   cd Moscow_transport_hakathon
   # распакуйте архив датасета в ./dataset, чтобы появился файл dataset/validate/traffic.csv
   docker compose up -d --build
   docker compose ps          # ml и backend должны стать healthy (около минуты)

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Что
     - Адрес
   * - Дашборд
     - http://localhost:3000
   * - Swagger backend
     - http://localhost:8000/docs
   * - Swagger ML-ядра
     - http://localhost:8001/docs
   * - Состояние системы
     - http://localhost:8000/health
   * - Приём NDTP (TCP)
     - ``localhost:9201``

Датасет лежит в другом месте? Укажите путь: ``DATASET_DIR=/path/to/dataset docker compose up -d``.
Датасетные часы задаются переменными ``CLOCK_START`` (по умолчанию ``2026-01-06T08:00:00``) и ``CLOCK_SPEED``
(ускорение, по умолчанию 1).


3. Как подать поток
-------------------

Вариант А. Исторический датасет (включён по умолчанию)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Ничего делать не нужно. Сразу после старта backend проигрывает ``validate/traffic.csv`` синхронно с датасетными
часами и заливает последний час истории, так что прогнозы появляются в первые минуты. Проверить можно так:
``GET /ingest/vehicles`` показывает ТС с источником ``replay``.

Вариант Б. Эмулятор NDTP организаторов
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   docker load -i dataset/ndtp-telemetry-emulator.tar     # один раз
   docker compose --profile emulator up -d                # эмулятор, управление на :18080

**Реальные треки (рекомендуем).** Скрипт раз в 5 секунд передаёт эмулятору координаты 13 реальных ТС из
``validate/traffic.csv``, а эмулятор шлёт их в backend настоящими NDTP-пакетами:

.. code-block:: bash

   pip install httpx numpy pandas
   python infra/emulator_replay.py --dataset ./dataset --emu-url http://localhost:18080 \
       --target-host backend --target-port 9201 --clock-url http://localhost:8000

Через минуту в ``GET /ingest/vehicles`` у этих ТС источник сменится на ``ndtp``, а в ``/health`` вырастут
``ndtp.units_connected`` и ``ndtp.fixes_total``.

**Случайное блуждание (autoGenerate).** В этом режиме эмулятор генерирует ``unitId``, которых нет в датасете.
Backend принимает и разбирает такие пакеты (растут ``ndtp.fixes_total`` и ``ingest.unknown_units``), но
сопоставить их с расписанием не может:

.. code-block:: bash

   curl -X POST http://localhost:18080/api/config \
       -H 'Content-Type: application/json' -d @infra/emulator-config.json

Вариант В. Свой поток на развёрнутый сервер
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Тот же скрипт работает и с сервером. Эмулятор запускается локально, а пакеты уходят на ``ndtp.mowtransit.ru:9201``:

.. code-block:: bash

   docker run -d --rm -p 18080:18080 --name ndtp-emu ndtp-telemetry-emulator:1.0
   python infra/emulator_replay.py --dataset ./dataset --emu-url http://localhost:18080 \
       --target-host ndtp.mowtransit.ru --target-port 9201 --clock-url https://api.mowtransit.ru

Если поток по какому-то ТС прекращается, через 60 секунд backend сам возвращает это ТС на проигрывание датасета.


4. Где смотреть прогнозы и алерты
---------------------------------

**Дашборд.** Цвет ТС показывает риск опоздания на целевой остановке. Лента алертов содержит только ТС, для которых
вероятность опоздать больше чем на 2 минуты составляет не меньше 0.7. Шкала «Ближайшие 15 минут» показывает, где
и когда ожидаются опоздания. Ползунок под картой перематывает назад то, что происходило с момента открытия страницы (до часа).

**API** (в Swagger каждый запрос можно выполнить кнопкой *Try it out*):

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Запрос
     - Что возвращает
   * - ``GET /predictions``
     - последний прогноз по каждому ТС: момент ``T``, целевая остановка, упреждение ``lead_s``, ``horizon_ok``,
       ``delay_pred_s``, интервал, ``risk_level``, причины, рекомендация
   * - ``GET /predictions/recent``
     - поток последних прогнозов
   * - ``GET /alerts?active=true``
     - активные алерты; ``active=false`` — закрытые: сверенные с фактом прибытия (оправдался ли) и снятые
   * - ``GET /state/vehicles``
     - производные признаки по ТС: текущее отклонение, скорость на сегменте, время простоя
   * - ``GET /state/vehicles/{tr_id}/arrivals``
     - прибытия на остановки, найденные по GPS
   * - ``GET /ingest/vehicles``
     - последний пакет по каждому ТС и его источник (``ndtp`` / ``replay``)
   * - ``GET /history/summary``, ``GET /history/alerts``
     - история из Postgres, сохраняется при перезапуске
   * - WebSocket ``/ws``
     - ``vehicle.update`` раз в секунду, ``alert.*`` сразу при появлении

Полный пример запроса к ML и ответа на него — ``ml/examples/predict_example.json``, поля описаны в
:doc:`inference_api`.


5. Метрики
----------

``GET /health`` — сводка системы, считается на лету:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Поле
     - Смысл
   * - ``ndtp.units_connected``, ``fixes_total``, ``fixes_dropped``, ``queue_depth``
     - живой NDTP-поток, потери и очередь
   * - ``predictor.horizon_ok_share``
     - доля прогнозов, у которых целевая остановка в окне (T+10, T+15] мин
   * - ``predictor.predictions_ml`` / ``predictions_fallback``
     - прогнозы от модели и запасные (если ML был недоступен)
   * - ``predictor.ml_batch_ms_p50`` / ``p95``
     - время ответа ML на пачку ТС
   * - ``alerts.raised`` / ``verified`` / ``precision`` / ``mae_verified_s``
     - алерты, сверенные с фактическим прибытием: доля оправдавшихся и ошибка прогноза
   * - ``history.written`` / ``pending`` / ``dropped``
     - запись в Postgres

``GET /metrics/model`` — качество модели: MAE по схемам валидации, живая latency, покрытие интервала.

Замеры точности, скорости, нагрузочного теста NDTP и деградации с командами для воспроизведения лежат в
репозитории: ``ml/PERFORMANCE.md`` и ``backend/README.md``.


6. Проверка надёжности (локально)
---------------------------------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Действие
     - Что происходит
   * - ``docker compose stop ml``
     - backend прогнозирует по текущему отклонению (``source: fallback``), ``/health`` → ``degraded``,
       дашборд работает; ``docker compose start ml`` — через ~30 с снова прогнозы модели
   * - остановить ``emulator_replay.py`` или ``docker compose stop ndtp-emu``
     - через 60 с ТС переходят на проигрывание датасета; после реконнекта снова идут по NDTP
   * - ``docker compose stop postgres``
     - история копится в памяти (``history.pending``); после ``start`` дописывается без потерь
   * - ``docker compose restart backend``
     - холодный старт за секунды, история последнего часа восстанавливается из датасета
