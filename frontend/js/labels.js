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
  // Синхронизировано с ml/configs/feature_labels_ml.json (FEATURES из ml/src/features/tabular.py).
  features: {
    cur_dev_s: "Текущее отклонение от графика, сек",
    lead_s: "До плановой остановки, сек",
    n_stops_between: "Остановок между текущей и целевой",

    plan_gap_prev_s: "План: разрыв до предыдущей остановки, сек",
    plan_gap_next_s: "План: разрыв до следующей остановки, сек",
    tgt_plan_gap_prev_s: "План: разрыв до целевой от предыдущей, сек",
    plan_route_dist_m: "Плановое расстояние до цели, м",
    plan_speed_kmh: "Плановая средняя скорость, км/ч",

    tgt_manual_fill: "У целевой остановки факт отмечен вручную",
    prev_manual_fill: "У предыдущей остановки факт отмечен вручную",
    mf_share_next: "Доля ручных отметок факта впереди",
    mf_share_trip: "Доля ручных отметок факта в рейсе",

    n_trip_breaks: "Разрывов плана > 5 мин на маршруте",
    tgt_first_in_trip: "Цель — первая остановка нового рейса",
    tgt_idx_in_trip: "Порядковый номер цели в рейсе",
    tgt_left_in_trip: "Остановок до конца рейса от цели",
    cur_idx_in_trip: "Текущая позиция ТС в рейсе",
    break_gap_s: "Разрыв до следующего рейса, сек",
    plan_gap_break_to_tgt_s: "От смены рейса до цели по плану, сек",

    gps_n: "GPS-пингов за окно",
    gps_last_dev: "Задержка на прошлой остановке по GPS, сек",
    gps_med3: "Медиана GPS-задержек за 3 остановки, сек",
    gps_med5: "Медиана GPS-задержек за 5 остановок, сек",
    gps_max5: "Максимум GPS-задержек за 5 остановок, сек",
    gps_min5: "Минимум GPS-задержек за 5 остановок, сек",
    gps_slope: "Тренд GPS-задержек (растёт/падает)",
    gps_age_s: "Возраст последнего GPS-пинга, сек",
    gps_last_dwell_s: "Стоянка на прошлой остановке по GPS, сек",
    gps_dev_minus_cur: "Насколько GPS-задержка свежее плановой",

    overdue_s: "Просрочка последнего пинга, сек",
    overdue_n: "Число просрочек за окно",

    gps_n_trip: "GPS-пингов на текущем рейсе",
    gps_trip_first_dev: "GPS-задержка на первой остановке рейса, сек",

    spd1: "Скорость за 1 мин, м/с",
    spd5: "Скорость за 5 мин, м/с",
    spd15: "Скорость за 15 мин, м/с",
    spd_std5: "Разброс скорости за 5 мин",
    stop5: "Доля простоя за 5 мин",
    stop15: "Доля простоя за 15 мин",
    spd_trend: "Тренд скорости (ускоряется/замедляется)",
    moving_spd15: "Скорость в движении за 15 мин, м/с",
    disp5_m: "Пройденное расстояние за 5 мин, м",
    disp15_m: "Пройденное расстояние за 15 мин, м",
    n_pkt15: "Пакетов телеметрии за 15 мин",
    last_fix_age_s: "Возраст последнего валидного GPS, сек",
    last_speed: "Последняя измеренная скорость, м/с",

    dist_tgt_m: "Расстояние до цели по GPS, м",
    dist_next_stop_m: "Расстояние до ближайшей остановки, м",
    route_left_m: "Осталось по маршруту, м",

    eta_dev_s: "ETA-прогноз: отклонение от плана, сек",
    eta_dev_moving_s: "ETA (в движении): отклонение, сек",
    req_speed_kmh: "Требуемая скорость чтобы успеть, км/ч",

    hour: "Час дня",
    hour_sin: "Час дня (циклическая компонента)",
    hour_cos: "Час дня (циклическая компонента)",
    route: "Маршрут",

    // Старые/альтернативные имена, оставлены на совместимость
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
App.esc = function (s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
};
