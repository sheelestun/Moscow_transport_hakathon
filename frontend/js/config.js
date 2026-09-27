// Настройки дашборда.
// Любую настройку можно переопределить через адресную строку, например:
//   index.html?mode=live&api=http://localhost:8000&ws=ws://localhost:8000/ws
// В продакшене nginx-контейнер подмешивает API_BASE/WS_URL через envsubst
// (docker-compose передаёт переменные окружения, см. frontend/entrypoint.sh).
window.App = window.App || {};

// Текущее время. Мок подменяет его своими «ускоренными» часами.
App.now = () => Date.now();

// Значения, подставляемые envsubst при старте контейнера.
// Плейсхолдеры остаются как есть в dev-режиме (открытие index.html напрямую) —
// в этом случае используются fallback-константы ниже.
const RUNTIME_MODE = "__MODE__";
const RUNTIME_API_BASE = "__API_BASE__";
const RUNTIME_WS_URL = "__WS_URL__";

const _pick = (val, fallback) => (val && !val.startsWith("__") ? val : fallback);

App.config = (function () {
  const p = new URLSearchParams(location.search);
  return {
    // "mock" — фейковые данные прямо в браузере (бэкенд не нужен)
    // "live" — настоящий бэкенд Даниила (REST + WebSocket)
    MODE: p.get("mode") || _pick(RUNTIME_MODE, "mock"),
    API_BASE: p.get("api") || _pick(RUNTIME_API_BASE, "http://localhost:8000"),
    WS_URL: p.get("ws") || _pick(RUNTIME_WS_URL, "ws://localhost:8000/ws"),

    // Пороги светофора по risk_score (0..1), который приходит от ML
    RISK_RED: 0.7,
    RISK_YELLOW: 0.35,

    MAP_CENTER: [55.73, 37.57],
    MAP_ZOOM: 12,
    // ТС с более сильным отклонением от геометрии своего маршрута не показываем на карте.
    // Большой допуск оставляет видимыми GPS-шум, соседнюю полосу и короткие объезды.
    OFF_ROUTE_HIDE_M: 300,
    TIMEZONE: "Europe/Moscow",
  };
})();
