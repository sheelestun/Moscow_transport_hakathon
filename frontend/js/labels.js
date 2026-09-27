// Человеческие подписи и форматирование.
// Бэкенд и ML присылают коды ("traffic_jam_ahead"), а диспетчеру показываем русский текст.
window.App = window.App || {};

App.labels = {
  reasons: {
    traffic_jam_ahead: "Пробка впереди по маршруту",
    long_dwell: "Долгая посадка на остановках",
    speed_drop: "Резкое падение скорости перед перекрёстком",
    bunching: "Сбой интервала: ТС догоняет соседнее",
    accumulated_delay: "Накопленное опоздание с прошлых остановок",
  },
  recommendations: {
    add_reserve: "Выпустить резервное ТС",
    release_reserve: "Выпустить резервное ТС",
    adjust_interval: "Скорректировать интервалы на маршруте",
    detour: "Предложить объезд проблемного участка",
    hold_at_stop: "Придержать следующее ТС на остановке",
    signal_priority: "Дать приоритет на светофорах",
  },
  features: {
    cur_dev_s: "Текущее отклонение от графика",
    current_delay_sec: "Задержка сейчас",
    speed_avg_5min: "Средняя скорость за 5 мин",
    speed_avg_15min: "Средняя скорость за 15 мин",
    dwell_last_stop_sec: "Стоянка на прошлой остановке",
    headway_to_next_sec: "Интервал до следующего ТС",
    headway_to_prev_sec: "Интервал до предыдущего ТС",
    distance_to_target_m: "Расстояние до остановки",
    planned_time_to_target_sec: "Плановое время в пути",
    hour_of_day: "Час дня",
    day_of_week: "День недели",
    weather_code: "Погода",
    traffic_score: "Загруженность дорог",
  },
  // Пояснение причины человеческим языком (для карточки ТС)
  explanations: {
    traffic_jam_ahead: "Впереди по маршруту затор: за последние 5 минут скорость заметно ниже плановой, и по данным о загруженности дорог лучше не станет.",
    long_dwell: "На последних остановках ТС стоит дольше обычного: большой пассажиропоток или задержка с посадкой.",
    speed_drop: "Скорость резко падает на подходах к перекрёсткам. Похоже на задержки на светофорах.",
    bunching: "Интервал до соседнего ТС сократился: машины идут «пачкой», и это ТС собирает больше пассажиров.",
    accumulated_delay: "Опоздание накопилось на предыдущих участках и пока не отыгрывается.",
  },
  // Сценарии What-if — те же коды, что в ML-сервисе (WHATIF_DELTA_MAP в ml/src/inference_service.py)
  // Виды транспорта (поле transport_type у маршрута) — в том порядке, как показываем в меню
  transport: {
    bus: "Автобусы",
    electrobus: "Электробусы",
    trolleybus: "Троллейбусы",
    tram: "Трамваи",
  },
  scenarios: {
    add_reserve: "Выпустить резервное ТС",
    adjust_interval: "Скорректировать интервалы",
    detour: "Пустить в объезд",
    signal_priority: "Приоритет на светофорах",
    hold_at_stop: "Придержать на остановке",
  },

  // Перевод кода в текст; если перевода нет — показываем код как есть
  t(dict, code) {
    return (this[dict] && this[dict][code]) || code || "—";
  },
};

// Код рекомендации -> код сценария What-if (release_reserve — старое имя add_reserve)
App.toScenario = function (rec) {
  return rec === "release_reserve" ? "add_reserve" : rec;
};

// ---------- Уровень риска ----------

// risk_score (0..1) -> "red" | "yellow" | "green"
App.riskLevel = function (risk) {
  if (risk >= App.config.RISK_RED) return "red";
  if (risk >= App.config.RISK_YELLOW) return "yellow";
  return "green";
};

App.levelName = { red: "Опоздает", yellow: "Риск", green: "По графику" };

// Уровень по величине опоздания в секундах (через ту же формулу, что risk_score)
App.delayLevel = function (sec) {
  return App.riskLevel(1 / (1 + Math.exp(-((sec || 0) - 120) / 60)));
};

// ---------- Форматирование ----------

// 187 -> "+3 мин 07 с",  -45 -> "−45 с"
App.fmtDelay = function (sec) {
  if (sec == null || isNaN(sec)) return "—";
  const sign = sec > 0 ? "+" : sec < 0 ? "−" : "";
  const s = Math.round(Math.abs(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m === 0) return `${sign}${r} с`;
  return `${sign}${m} мин ${String(r).padStart(2, "0")} с`;
};

// Короткий вариант: 187 -> "+3:07"
App.fmtDelayShort = function (sec) {
  if (sec == null || isNaN(sec)) return "—";
  const sign = sec > 0 ? "+" : sec < 0 ? "−" : "";
  const s = Math.round(Math.abs(sec));
  return `${sign}${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// Время по Москве: "14:47"
App.fmtTime = function (iso, withSeconds) {
  const d = iso instanceof Date ? iso : new Date(iso);
  return d.toLocaleTimeString("ru-RU", {
    timeZone: App.config.TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
  });
};

// "через 12 мин" / "сейчас" / "3 мин назад"
App.fmtIn = function (iso) {
  const min = Math.round((new Date(iso) - App.now()) / 60000);
  if (min > 0) return `через ${min} мин`;
  if (min === 0) return "сейчас";
  return `${-min} мин назад`;
};

App.pct = function (x) {
  return `${Math.round((x || 0) * 100)}%`;
};

// Защита от HTML-инъекций при вставке текста в innerHTML
// Подпись маршрута для «фишки»: настоящий номер из 2ГИС (route_number), если backend его прислал, иначе route_id.
// app.js переопределяет её, когда загружены маршруты; здесь — безопасное значение по умолчанию.
App.routeLabel = App.routeLabel || function (routeId) { return routeId; };

App.esc = function (s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
};
