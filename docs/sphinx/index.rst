Предиктор задержек наземного транспорта Москвы
================================================

Система заранее предупреждает диспетчера об опоздании автобуса: за 10–15 минут до остановки, с причиной и
рекомендацией. Она слушает NDTP-телеметрию, сопоставляет её с расписанием и прогнозирует задержку
CatBoost-ансамблем.

- **Жюри** — начните с раздела :doc:`getting_started`: как подать поток, где смотреть прогнозы, алерты и метрики.
- **Дашборд**: https://app.mowtransit.ru
- **OpenAPI / Swagger backend**: https://api.mowtransit.ru/docs; API ML-ядра описан в :doc:`inference_api`
- **Исходный код**: https://github.com/sheelestun/Moscow_transport_hakathon

.. toctree::
   :maxdepth: 2
   :caption: Содержание:

   getting_started
   overview
   ml_module
   inference_api
   backend_module
   modules

Индексы и таблицы
-----------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
