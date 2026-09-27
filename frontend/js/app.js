// Главный файл: хранит состояние и связывает источник данных, карту и панель.
window.App = window.App || {};

(function (App) {
  const cfg = App.config;
  const source = App.createSource(cfg);
  const $ = (id) => document.getElementById(id);

  // ---------- Состояние ----------
  const state = {
    routes: new Map(),   // route_id -> route
    vehicles: new Map(), // vehicle_id -> vehicle
    alerts: new Map(),   // alert_id -> alert
    acked: new Set(),    // алерты, которые диспетчер отметил «Принято»
    whatif: new Map(),   // vehicle_id -> последний результат What-if
    routeHours: new Map(),// route_id -> {first_hhmm, last_hhmm, next_departure, ...} (кэш, /routes/{id}/hours)
    selectedId: null,    // выбранное ТС
    selectedToken: 0,    // растёт при каждой смене selectedId — ловим устаревшие ответы fetch
    schedule: null,      // расписание выбранного ТС
    status: "connecting",
    lastUpdate: null,
    riskMode: false,
    applied: [],         // применённые меры What-if
    lines: loadLines(),
    verified: [],        // сверенные прогнозы (прогноз vs факт)  // выбранные линии: Set route_id или null = все
  };

  // ---------- Выбор линий (запоминаем в браузере) ----------
  function loadLines() {
    try {
      const raw = localStorage.getItem("dispatcher.lines");
      return raw ? new Set(JSON.parse(raw)) : null;
    } catch { return null; }
  }
  function saveLines() {
    try {
      if (state.lines) localStorage.setItem("dispatcher.lines", JSON.stringify([...state.lines]));
      else localStorage.removeItem("dispatcher.lines");
    } catch {}
  }
  const isVisible = (routeId) => !state.lines || state.lines.has(routeId);
  const visibleVehicles = () => [...state.vehicles.values()].filter((v) => isVisible(v.route_id));

  // ---------- Производные данные ----------
  const activeAlerts = () =>
    [...state.alerts.values()].filter((a) => !state.acked.has(a.alert_id) && isVisible(a.route_id)).sort((a, b) => b.risk_score - a.risk_score);

  const alertFor = (vehicleId) => activeAlerts().find((a) => a.vehicle_id === vehicleId);

  function routeStats() {
    const stats = {};
    for (const r of state.routes.values()) if (isVisible(r.route_id)) stats[r.route_id] = { route_id: r.route_id, name: r.name, total: 0, red: 0, yellow: 0 };
    for (const v of state.vehicles.values()) {
      const s = stats[v.route_id];
      if (!s) continue;
      s.total++;
      const l = App.riskLevel(v.risk_score);
      if (l === "red") s.red++;
      if (l === "yellow") s.yellow++;
    }
    const rank = { red: 0, yellow: 1, green: 2 };
    return Object.values(stats)
      .map((s) => ({ ...s, level: s.red ? "red" : s.yellow ? "yellow" : "green" }))
      .sort((a, b) => rank[a.level] - rank[b.level] || b.red - a.red || b.yellow - a.yellow);
  }

  // ---------- Отрисовка ----------
  function renderKpis() {
    const c = { red: 0, yellow: 0, green: 0 };
    const vs = visibleVehicles();
    for (const v of vs) c[App.riskLevel(v.risk_score)]++;
    $("kpi-total").textContent = vs.length;
    $("kpi-red").textContent = c.red;
    $("kpi-yellow").textContent = c.yellow;
    $("kpi-green").textContent = c.green;
  }

  function renderStatus() {
    $("status").className = "status status--" + state.status;
    $("status-text").textContent = {
      connecting: "подключение…",
      live: "онлайн",
      mock: "демо-данные",
      degraded: "нет связи · данные на " + (state.lastUpdate ? App.fmtTime(state.lastUpdate, true) : "—"),
    }[state.status];
  }

  function renderList() {
    const hidden = [...state.alerts.values()].filter((a) => !state.acked.has(a.alert_id) && !isVisible(a.route_id));
    App.sidebar.renderAlerts(activeAlerts(), {
      onSelect: selectVehicle,
      noLines: !!state.lines && state.lines.size === 0,
      hidden: { count: hidden.length, routes: [...new Set(hidden.map((a) => a.route_id))] },
      onShowAll: () => setLines(null),
    });
  }

  function renderRoutes() {
    const stats = routeStats();
    App.sidebar.renderRoutes(stats, {
      onSelect: (id) => {
        // клик по маршруту: выбрать самое проблемное ТС на нём
        const vs = [...state.vehicles.values()].filter((v) => v.route_id === id).sort((a, b) => b.risk_score - a.risk_score);
        if (vs[0]) selectVehicle(vs[0].vehicle_id);
      },
    });
    App.map.setRouteLevels(Object.fromEntries(stats.map((s) => [s.route_id, s.level])));
  }

  async function renderWorstStops() {
    if (!source.getWorstStops) return;
    const el = $("worst-stops");
    if (!el) return;
    try {
      const stops = await source.getWorstStops(10);
      if (!stops || !stops.length) {
        el.innerHTML = `<li class="empty empty--muted">Проблемных точек не найдено.</li>`;
        return;
      }
      el.innerHTML = stops.map((s) => {
        const level = App.delayLevel ? App.delayLevel(s.avg_delay_sec) : (s.avg_delay_sec >= 180 ? "red" : "yellow");
        return `<li class="worst-stop">
          <span class="worst-stop__delay t-${level}">+${Math.round(s.avg_delay_sec)}с</span>
          <span class="worst-stop__body">
            <b>${App.esc(s.name)}</b>
            <span class="muted">${App.esc(s.route_id)} · ${s.vehicles} ТС · пик ${Math.round(s.max_delay_sec)}с</span>
          </span>
        </li>`;
      }).join("");
    } catch {
      el.hidden = true;
    }
  }

  function renderLeadBars(mae_by_lead) {
    const el = $("lead-bars");
    if (!el) return;
    if (!Array.isArray(mae_by_lead) || !mae_by_lead.length) { el.hidden = true; return; }
    const max = Math.max(...mae_by_lead.map((r) => r.mae_s || 0), 1);
    el.innerHTML = mae_by_lead.map((r) => {
      const pct = Math.max(3, Math.round((r.mae_s / max) * 100));
      return `<div class="lead-bar" title="${App.esc(r.bin)}: MAE ${Math.round(r.mae_s)} с${r.points ? ` (n=${r.points})` : ""}">
        <span class="lead-bar__lab">${App.esc(r.bin)}</span>
        <span class="lead-bar__track"><i style="width:${pct}%"></i></span>
        <span class="lead-bar__val">${Math.round(r.mae_s)} с</span>
      </div>`;
    }).join("");
  }

  async function renderBunching() {
    if (!source.getBunching) return;
    const el = $("bunching");
    if (!el) return;
    try {
      const pairs = await source.getBunching();
      if (!pairs || !pairs.length) {
        el.innerHTML = `<li class="empty empty--muted">Слипаний не обнаружено.</li>`;
        App.map.setBunchingPairs && App.map.setBunchingPairs([]);
        return;
      }
      el.innerHTML = pairs.slice(0, 6).map((p) => {
        const gap = Math.max(0, Math.round(p.headway_sec));
        const plan = Math.max(0, Math.round(p.plan_headway_sec));
        const pct = Math.round((p.ratio || 0) * 100);
        return `<li class="worst-stop" data-pair="${App.esc(p.leader_id)}|${App.esc(p.follower_id)}">
          <span class="worst-stop__delay t-red">${gap}с</span>
          <span class="worst-stop__body">
            <b>${App.esc(p.route_id)} · пара ${App.esc(p.leader_id)} → ${App.esc(p.follower_id)}</b>
            <span class="muted">интервал ${gap}с при плане ${plan}с (${pct}% от плана)</span>
          </span>
        </li>`;
      }).join("");
      App.map.setBunchingPairs && App.map.setBunchingPairs(pairs);
    } catch {
      el.hidden = true;
    }
  }

  function renderHourlyHeatmap() {
    const el = $("heatmap");
    if (!el || !App.HOURLY_STATS) return;
    const values = Object.values(App.HOURLY_STATS).map((h) => h.mae).filter((v) => Number.isFinite(v));
    if (!values.length) { el.hidden = true; return; }
    const min = Math.min(...values), max = Math.max(...values);
    const nowHour = new Date().getHours();
    const cells = Object.entries(App.HOURLY_STATS).map(([h, s]) => {
      const hasData = Number.isFinite(s.mae);
      const t = hasData ? Math.max(0, Math.min(1, (s.mae - min) / (max - min || 1))) : 0;
      const alpha = hasData ? 0.15 + 0.75 * t : 0.05;
      const isNow = Number(h) === nowHour;
      const label = hasData ? `${Math.round(s.mae)}с` : "—";
      const tip = hasData
        ? `${h}:00 — среднее опоздание ${s.mae}с (n=${s.points})`
        : `${h}:00 — данных нет`;
      return `<div class="heat-cell ${isNow ? "heat-cell--now" : ""}" title="${tip}" style="background:rgba(255,90,82,${alpha.toFixed(2)})"><b>${h}</b><i>${label}</i></div>`;
    }).join("");
    el.innerHTML = cells;
  }

  function renderVehicle() {
    if (!state.selectedId) return;
    const v = state.vehicles.get(state.selectedId);
    App.sidebar.updateVehicle({
      vehicle: v,
      route: state.routes.get(v.route_id),
      alert: alertFor(v.vehicle_id),
      schedule: state.schedule,
      whatif: state.whatif.get(v.vehicle_id),
      hours: state.routeHours.get(v.route_id),
    });
  }

  // Кэш «часов работы» маршрута: дёргаем /routes/{id}/hours однажды при выборе ТС и запоминаем.
  // Отсутствие endpoint'а (мок / старый бэкенд) — молча пропускаем.
  async function loadRouteHours(routeId) {
    if (!source.getRouteHours || state.routeHours.has(routeId)) return;
    state.routeHours.set(routeId, null); // маркер «уже запросили», чтобы не дублировать
    try {
      const h = await source.getRouteHours(routeId);
      if (h) state.routeHours.set(routeId, h);
    } catch { /* тихо */ }
  }

  // ---------- Выбор ТС ----------
  async function selectVehicle(id) {
    const v = state.vehicles.get(id);
    if (!v) return;
    state.selectedId = id;
    state.selectedToken += 1;
    state.schedule = null;
    const route = state.routes.get(v.route_id);

    App.sidebar.openVehicle({
      onBack: clearSelection,
      onAck: () => {
        const a = alertFor(id);
        if (a) state.acked.add(a.alert_id);
        renderVehicle();
      },
      canApply: !!source.applyMeasure, // «Применить» есть только в демо-симуляции
      isApplied: (routeId) => lastApplied(routeId),
      onRecEffect: (scenario) => {
        const cur = state.vehicles.get(id);
        const a = alertFor(id);
        return source.whatif({ scenario, route_id: cur.route_id, at_stop_id: a ? a.target_stop_id : null });
      },
      onApply: (scenario) => applyMeasure(scenario, state.vehicles.get(id).route_id),
      onWhatifOpen: () => {
        const cur = state.vehicles.get(id);
        const a = alertFor(id);
        App.whatif.open({
          source,
          vehicle: cur,
          route: state.routes.get(cur.route_id),
          alert: a,
          rec: App.toScenario((a && a.recommendation) || cur.recommendation),
          onApply: (scenario) => applyMeasure(scenario, cur.route_id),
        });
      },
    });
    renderVehicle();
    App.map.focusRoute(route, v);
    loadRouteHours(v.route_id).then(() => renderVehicle());
    await refreshSchedule();
  }

  async function refreshSchedule() {
    const id = state.selectedId;
    if (!id) return;
    // Токен фиксируем ДО фетча — если за время запроса пользователь переключил ТС
    // (даже на то же самое, но с промежуточным clearSelection), поймаем это.
    const token = state.selectedToken;
    try {
      const sch = await source.getSchedule(id);
      if (state.selectedToken !== token || state.selectedId !== id) return;
      const first = !state.schedule;
      state.schedule = sch;
      const v = state.vehicles.get(id);
      if (!v) return; // ТС исчезло из state.vehicles между запросом и ответом
      if (first) App.map.select(v, state.routes.get(v.route_id), sch);
      else App.map.updateSelection(v, state.routes.get(v.route_id), sch);
      renderVehicle();
    } catch (e) {
      console.warn("Расписание не загрузилось:", e);
    }
  }

  function clearSelection() {
    state.selectedId = null;
    state.selectedToken += 1;
    state.schedule = null;
    App.sidebar.closeVehicle();
    App.map.clearSelection();
    renderList();
  }

  // ---------- События из источника данных ----------
  const handlers = {
    onStatus(s) {
      const was = state.status;
      state.status = s;
      renderStatus();
      renderOffline();
      if (was === "degraded" && s !== "degraded") toast("Связь восстановлена. Данные снова обновляются в реальном времени.");
    },
    onVehicles(list) {
      for (const v of list) state.vehicles.set(v.vehicle_id, { ...state.vehicles.get(v.vehicle_id), ...v });
      state.lastUpdate = new Date(App.now());
      App.map.updateVehicles(list);
      renderKpis();
      // Светофоры (фазы есть только в демо-симуляции): на всех видимых линиях,
      // у выбранного автобуса — крупнее
      if (source.getSignals) {
        const sv = state.selectedId && state.vehicles.get(state.selectedId);
        const list = [];
        for (const r of state.routes.values()) {
          if (!isVisible(r.route_id)) continue;
          const sel = !!sv && sv.route_id === r.route_id;
          source.getSignals(r.route_id).forEach((s) => list.push({ ...s, sel }));
        }
        App.map.updateSignals(list);
      }
      renderVehicle();
    },
    onAlertNew(a) {
      a._fresh = true;
      state.alerts.set(a.alert_id, a);
      setTimeout(() => (a._fresh = false), 4000);
      renderList();
    },
    onAlertResolved(id) {
      state.alerts.delete(id);
      renderList();
    },
    onAlertVerified(v) {
      state.alerts.delete(v.alert_id);
      // была ли применена мера на этом маршруте после появления алерта
      const measure = lastApplied(v.route_id);
      state.verified.unshift({ ...v, measure });
      state.verified = state.verified.slice(0, 50);
      renderList();
      renderVerified();
    },
  };

  // ---------- Шкала «ближайшие 15 минут» ----------
  const HORIZON_MIN = 15;
  const hzItems = new Map(); // alert_id -> элемент (переиспользуем, чтобы не терялись клики)
  function renderHorizon() {
    const track = $("horizon");
    if (!track.querySelector(".hz-axis")) {
      let html = `<div class="hz-axis"></div>`;
      for (let m = 0; m <= HORIZON_MIN; m += 5) {
        html += `<span class="hz-tick" style="left:${(m / HORIZON_MIN) * 100}%">${m === 0 ? "сейчас" : "+" + m + " мин"}</span>`;
      }
      html += `<span class="hz-empty" hidden>Инцидентов в ближайшие 15 минут не ожидается</span>`;
      track.insertAdjacentHTML("beforeend", html);
    }
    const now = App.now();
    const items = activeAlerts()
      .map((a) => ({ a, min: (new Date(a.eta_incident).getTime() - now) / 60000 }))
      .filter((x) => x.min >= -0.5 && x.min <= HORIZON_MIN + 0.5)
      .sort((x, y) => x.min - y.min);
    track.querySelector(".hz-empty").hidden = items.length > 0;
    $("horizon-count").textContent = items.length ? `${items.length} ${plural(items.length, "инцидент", "инцидента", "инцидентов")}` : "";
    // Строка для свёрнутого вида
    const first = items.find((x) => x.min >= 0);
    $("horizon-summary").innerHTML = !items.length
      ? `<span class="t-green">инцидентов не ожидается</span>`
      : `<b class="t-red">${items.length} ${plural(items.length, "инцидент", "инцидента", "инцидентов")}</b>` +
        (first ? ` · ближайший через <b>${Math.max(0, Math.round(first.min))} мин</b>: <b>${App.esc(first.a.route_id)}</b> ${App.fmtDelayShort(first.a.delay_pred_sec)} к «${App.esc(first.a.target_stop_name || first.a.target_stop_id)}»` : "");
    if (document.body.classList.contains("hz-collapsed")) return; // в свёрнутом виде точки не раскладываем

    // создаём / обновляем элементы
    const alive = new Set();
    for (const { a, min } of items) {
      alive.add(a.alert_id);
      let el = hzItems.get(a.alert_id);
      if (!el) {
        el = document.createElement("div");
        el.className = "hz-item";
        el.innerHTML = `<button class="hz-chip"></button><i class="hz-stem"></i><i class="hz-dot"></i>`;
        el.querySelector(".hz-chip").onclick = () => selectVehicle(a.vehicle_id);
        track.appendChild(el);
        hzItems.set(a.alert_id, el);
      }
      const level = App.riskLevel(a.risk_score);
      el.dataset.level = level;
      el.classList.toggle("is-selected", a.vehicle_id === state.selectedId);
      const chip = el.querySelector(".hz-chip");
      chip.innerHTML = `<b>${App.esc(a.route_id)}</b><span>${App.fmtDelayShort(a.delay_pred_sec)}</span>`;
      chip.title = `ТС ${a.vehicle_id}, маршрут ${a.route_id}: ${App.fmtDelay(a.delay_pred_sec)} к «${a.target_stop_name || a.target_stop_id}» ${App.fmtIn(a.eta_incident)}`;
      el._min = min;
    }
    for (const [id, el] of hzItems) if (!alive.has(id)) { el.remove(); hzItems.delete(id); }

    // раскладка по «этажам», чтобы подписи не налезали друг на друга
    const W = track.clientWidth;
    const LANES = 3, LANE_H = 24, GAP = 6;
    const axisY = track.clientHeight - 22;
    const ends = new Array(LANES).fill(-Infinity);
    for (const { a } of items) {
      const el = hzItems.get(a.alert_id);
      const chip = el.querySelector(".hz-chip");
      const x = Math.max(0, Math.min(1, el._min / HORIZON_MIN)) * W;
      const w = chip.offsetWidth;
      const left = Math.max(0, Math.min(W - w, x - w / 2));
      let lane = ends.findIndex((e) => left > e + GAP);
      if (lane < 0) lane = ends.indexOf(Math.min(...ends));
      ends[lane] = left + w;
      const top = 4 + lane * LANE_H;
      el.style.left = x + "px";
      chip.style.left = left - x + "px";
      chip.style.top = top + "px";
      const stem = el.querySelector(".hz-stem");
      stem.style.top = top + 20 + "px";
      stem.style.height = Math.max(0, axisY - top - 24) + "px";
      el.querySelector(".hz-dot").style.top = axisY - 6 + "px";
    }
  }
  const plural = (n, one, few, many) =>
    n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;
  window.addEventListener("resize", () => renderHorizon());

  // Свернуть / развернуть шкалу (запоминаем в браузере)
  function initHorizonToggle() {
    const btn = $("horizon-toggle");
    const apply = (collapsed) => {
      document.body.classList.toggle("hz-collapsed", collapsed);
      btn.textContent = collapsed ? "Развернуть ▾" : "Свернуть ▴";
      btn.setAttribute("aria-expanded", String(!collapsed));
      App.map.resize();
      renderHorizon();
    };
    let collapsed = false;
    try { collapsed = localStorage.getItem("dispatcher.horizonCollapsed") === "1"; } catch {}
    apply(collapsed);
    btn.onclick = () => {
      collapsed = !collapsed;
      try { localStorage.setItem("dispatcher.horizonCollapsed", collapsed ? "1" : "0"); } catch {}
      apply(collapsed);
    };
  }

  // ---------- «Сбылись ли прогнозы» ----------
  const HIT_SEC = 90; // прогноз считаем сбывшимся, если ошибка не больше 1,5 минуты
  const CSV_HEADERS = ["alert_id", "route_id", "vehicle_id", "target_stop_id", "target_stop_name",
                       "eta_incident", "delay_pred_sec", "delay_fact_sec", "measure", "measure_at"];
  function csvEscape(v) {
    if (v == null) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function verifiedToCsv(list) {
    const rows = [CSV_HEADERS.join(",")];
    for (const v of list) {
      rows.push(CSV_HEADERS.map((h) => {
        if (h === "measure") return csvEscape(v.measure && v.measure.scenario);
        if (h === "measure_at") return csvEscape(v.measure && v.measure.at);
        return csvEscape(v[h]);
      }).join(","));
    }
    return rows.join("\r\n");
  }
  function downloadVerifiedCsv() {
    const list = state.verified.filter((v) => isVisible(v.route_id));
    if (!list.length) return;
    // BOM для корректной кодировки в Excel; RFC 4180 line endings.
    const blob = new Blob(["﻿" + verifiedToCsv(list)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    a.href = url;
    a.download = `verified-${ts}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }
  function renderVerified() {
    const list = state.verified.filter((v) => isVisible(v.route_id));
    const plain = list.filter((v) => !v.measure);
    const hits = plain.filter((v) => Math.abs(v.delay_fact_sec - v.delay_pred_sec) <= HIT_SEC).length;
    const dl = $("verified-download");
    if (dl) dl.hidden = list.length === 0;
    $("verified-score").innerHTML = plain.length
      ? `Точность за смену: <b class="${hits / plain.length >= 0.7 ? "t-green" : "t-yellow"}">${Math.round((hits / plain.length) * 100)}%</b> — сбылось ${hits} из ${plain.length} (ошибка до 1,5 мин)`
      : "Когда наступает время инцидента, сверяем прогноз с фактом.";
    $("verified").innerHTML = list.slice(0, 5).map((v) => {
      const err = v.delay_fact_sec - v.delay_pred_sec;
      let mark, cls;
      if (v.measure && v.delay_fact_sec < v.delay_pred_sec - 30) { mark = "мера помогла"; cls = "ok"; }
      else if (Math.abs(err) <= HIT_SEC) { mark = "сбылся"; cls = "ok"; }
      else { mark = "ошибка " + App.fmtDelayShort(Math.abs(err)).replace(/^[+−]/, ""); cls = "miss"; }
      return `
        <li class="vf vf--${cls}">
          <span class="route-chip">${App.esc(v.route_id)}</span>
          <span class="vf__stop">«${App.esc(v.target_stop_name || v.target_stop_id)}»</span>
          <span class="vf__nums">прогноз <b>${App.fmtDelayShort(v.delay_pred_sec)}</b> · факт <b>${App.fmtDelayShort(v.delay_fact_sec)}</b></span>
          <span class="vf__mark">${cls === "ok" ? "✓" : "✗"} ${mark}</span>
        </li>`;
    }).join("") || `<li class="empty empty--muted">Пока нечего сверять — первые результаты появятся через несколько минут.</li>`;
  }

  // ---------- Обрыв связи ----------
  function renderOffline() {
    const off = state.status === "degraded";
    $("offline").hidden = !off;
    document.body.classList.toggle("is-offline", off);
    if (off) $("offline-text").textContent =
      `Показано последнее известное состояние на ${state.lastUpdate ? App.fmtTime(state.lastUpdate, true) : "—"}. Переподключаемся…`;
  }

  // ---------- Меню «Линии» ----------
  // Какие маршруты подходят под поиск и выбранный вид транспорта
  let linesQuery = "";
  let linesType = "all";
  const norm = (x) => String(x || "").toLowerCase().replace(/ё/g, "е");
  function foundRoutes() {
    const q = norm(linesQuery.trim());
    return [...state.routes.values()].filter((r) =>
      (linesType === "all" || (r.transport_type || "other") === linesType) &&
      (!q || norm(r.route_id).includes(q) || norm(r.name).includes(q) ||
        (r.stops || []).some((st) => norm(st.name).includes(q))));
  }

  function renderLinesMenu() {
    const routes = [...state.routes.values()];
    const n = state.lines ? routes.filter((r) => state.lines.has(r.route_id)).length : routes.length;
    $("lines-label").textContent = !state.lines ? "все" : n === 0 ? "не выбраны" : `${n} из ${routes.length}`;
    const levels = Object.fromEntries(allRouteLevels().map((s) => [s.route_id, s.level]));

    // Вкладки видов транспорта — только если бэкенд/мок присылает transport_type
    const types = Object.keys(App.labels.transport).filter((t) => routes.some((r) => r.transport_type === t));
    const typesBox = $("lines-types");
    typesBox.hidden = types.length < 2;
    if (types.length >= 2) {
      const count = (t) => routes.filter((r) => t === "all" || r.transport_type === t).length;
      typesBox.innerHTML = ["all", ...types].map((t) => `
        <button class="type-chip ${t === linesType ? "is-on" : ""}" data-type="${t}" aria-pressed="${t === linesType}">
          ${t === "all" ? "Все виды" : App.labels.transport[t]} <span>${count(t)}</span>
        </button>`).join("");
      typesBox.querySelectorAll("[data-type]").forEach((b) => (b.onclick = () => { linesType = b.dataset.type; renderLinesMenu(); }));
    } else linesType = "all";

    // Список: сгруппирован по видам транспорта (если они есть)
    const found = foundRoutes();
    const filtering = !!linesQuery.trim() || linesType !== "all";
    $("lines-found").textContent = filtering ? `Найдено: ${found.length}` : "Какие линии показывать";
    $("lines-all").textContent = filtering ? "Отметить найденные" : "Отметить все";
    $("lines-none").textContent = filtering ? "Снять найденные" : "Снять все";

    const row = (r) => `
      <li><label class="line-opt">
        <input type="checkbox" value="${App.esc(r.route_id)}" ${isVisible(r.route_id) ? "checked" : ""}>
        <span class="dot dot--${levels[r.route_id] || "green"}" title="${App.levelName[levels[r.route_id] || "green"]}"></span>
        <span class="route-chip" data-type="${App.esc(r.transport_type || "")}">${App.esc(r.route_id)}</span>
        <span class="line-opt__name">${App.esc(r.name)}</span>
      </label></li>`;
    let html;
    if (!found.length) {
      html = `<p class="lines__empty">Ничего не нашлось. Попробуйте номер маршрута или название остановки.</p>`;
    } else if (types.length >= 2) {
      const groups = [...types, "other"].map((t) => [t, found.filter((r) => (r.transport_type || "other") === t)]).filter(([, l]) => l.length);
      html = groups.map(([t, list]) => {
        const on = list.filter((r) => isVisible(r.route_id)).length;
        return `
        <div class="line-group">
          <label class="line-group__head">
            <input type="checkbox" data-group="${t}" ${on === list.length ? "checked" : ""} ${on > 0 && on < list.length ? 'data-mixed="1"' : ""}>
            <span>${App.labels.transport[t] || "Другое"}</span><span class="muted">${on} из ${list.length}</span>
          </label>
          <ul>${list.map(row).join("")}</ul>
        </div>`;
      }).join("");
    } else {
      html = `<ul>${found.map(row).join("")}</ul>`;
    }
    const box = $("lines-list");
    box.innerHTML = html;

    const current = () => (state.lines ? new Set(state.lines) : new Set(routes.map((r) => r.route_id)));
    const commit = (set) => setLines(set.size === routes.length ? null : set);
    box.querySelectorAll("input[value]").forEach((cb) => (cb.onchange = () => {
      const set = current();
      cb.checked ? set.add(cb.value) : set.delete(cb.value);
      commit(set);
    }));
    // Галочка группы: включить/выключить сразу весь вид транспорта (из найденных)
    box.querySelectorAll("input[data-group]").forEach((cb) => {
      if (cb.dataset.mixed) cb.indeterminate = true;
      cb.onchange = () => {
        const set = current();
        found.filter((r) => (r.transport_type || "other") === cb.dataset.group)
          .forEach((r) => (cb.checked ? set.add(r.route_id) : set.delete(r.route_id)));
        commit(set);
      };
    });
  }

  // Уровни всех маршрутов (для точек в меню — видно проблемные, даже если линия скрыта)
  function allRouteLevels() {
    const out = {};
    for (const v of state.vehicles.values()) {
      const l = App.riskLevel(v.risk_score);
      const cur = out[v.route_id] || "green";
      out[v.route_id] = l === "red" || cur === "red" ? "red" : l === "yellow" || cur === "yellow" ? "yellow" : "green";
    }
    return Object.entries(out).map(([route_id, level]) => ({ route_id, level }));
  }

  function setLines(set) {
    state.lines = set;
    saveLines();
    App.map.setVisibleRoutes(set);
    if (state.selectedId && !isVisible(state.vehicles.get(state.selectedId)?.route_id)) clearSelection();
    renderLinesMenu();
    renderKpis();
    renderList();
    renderRoutes();
    renderHorizon();
    renderVerified();
    App.map.fitRoutes([...state.routes.values()].filter((r) => isVisible(r.route_id)));
  }

  function initLinesMenu() {
    const btn = $("lines-btn"), menu = $("lines-menu");
    const toggle = (open) => {
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      if (open) { renderLinesMenu(); $("lines-search").focus(); }
    };
    btn.onclick = (e) => { e.stopPropagation(); toggle(menu.hidden); };
    menu.onclick = (e) => e.stopPropagation();
    document.addEventListener("click", () => toggle(false));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggle(false); });
    const all = () => [...state.routes.values()].map((r) => r.route_id);
    // «Отметить/снять все» — действуют на найденные линии, если идёт поиск или выбран вид транспорта
    $("lines-all").onclick = () => {
      const set = state.lines ? new Set(state.lines) : new Set(all());
      foundRoutes().forEach((r) => set.add(r.route_id));
      setLines(set.size === all().length ? null : set);
    };
    $("lines-none").onclick = () => {
      const set = state.lines ? new Set(state.lines) : new Set(all());
      foundRoutes().forEach((r) => set.delete(r.route_id));
      setLines(set);
    };
    const search = $("lines-search");
    search.oninput = () => { linesQuery = search.value; renderLinesMenu(); };
    // Enter — оставить только найденные линии
    search.onkeydown = (e) => {
      if (e.key === "Enter") { const f = foundRoutes(); if (f.length) setLines(new Set(f.map((r) => r.route_id))); }
    };
  }

  // ---------- Применение меры (демо) ----------
  async function applyMeasure(scenario, routeId) {
    await source.applyMeasure({ scenario, route_id: routeId });
    state.applied.push({ scenario, route_id: routeId, at: App.now() });
    toast(scenario === "signal_priority"
      ? `Приоритет на светофорах включён на маршруте ${routeId} на 20 минут: светофоры дают зелёный автобусам. Смотрите на карту.`
      : `Применено: ${App.labels.scenarios[scenario]} на маршруте ${routeId}. Опоздания начнут отыгрываться.`);
    renderVehicle();
  }

  // Последняя мера на маршруте за 20 минут (по часам симуляции)
  function lastApplied(routeId) {
    const list = state.applied.filter((x) => x.route_id === routeId && App.now() - x.at < 20 * 60000);
    return list[list.length - 1] || null;
  }

  // ---------- Уведомление внизу экрана ----------
  let toastTimer;
  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 5000);
  }

  // ---------- Запуск ----------
  async function init() {
    renderStatus();
    const tickClock = () => ($("clock").textContent = App.fmtTime(new Date(App.now()), true));
    tickClock();
    setInterval(tickClock, 1000);

    // Карту не ждём: панель и данные показываем сразу, маршруты дорисуются, когда подложка загрузится
    App.map.init(cfg, { onVehicle: selectVehicle, onEmptyClick: () => state.selectedId && clearSelection() })
      .then(() => { renderRoutes(); if (state.selectedId) refreshSchedule(); });

    // Скорость симуляции — только для демо-данных
    const speedBox = $("speed");
    if (source.setSpeed) {
      speedBox.hidden = false;
      const mark = () => speedBox.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.x === source.getSpeed())));
      speedBox.querySelectorAll("button").forEach((b) => (b.onclick = () => { source.setSpeed(+b.dataset.x); mark(); }));
      mark();
    }

    // Переключатель «риск на маршрутах»
    $("risk-toggle").onchange = (e) => App.map.setRiskMode(e.target.checked);
    App.map.setRiskMode($("risk-toggle").checked);
    initLinesMenu();
    initHorizonToggle();
    const dl = $("verified-download");
    if (dl) dl.onclick = downloadVerifiedCsv;

    // Кнопка «Обрыв связи» — только в демо-режиме
    if (source.setOffline) {
      const nb = $("net-btn");
      nb.hidden = false;
      nb.onclick = () => {
        const off = !source.isOffline();
        source.setOffline(off);
        nb.textContent = off ? "Восстановить связь" : "Обрыв связи";
        nb.classList.toggle("is-on", off);
      };
    }
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (App.whatif.isOpen()) App.whatif.close(); // сначала закрываем окно What-if
      else if (state.selectedId) clearSelection();
    });

    try {
      const [routes, vehicles, alerts] = await Promise.all([source.getRoutes(), source.getVehicles(), source.getAlerts()]);
      routes.forEach((r) => state.routes.set(r.route_id, r));
      // выбранные ранее линии, которых больше нет, — забываем и чистим localStorage,
      // иначе после переименования route_id в бэкенде дашборд остаётся с пустой выборкой навсегда.
      if (state.lines) {
        const before = state.lines.size;
        const filtered = new Set([...state.lines].filter((id) => state.routes.has(id)));
        state.lines = filtered.size === 0 ? null : filtered;
        if (before !== (state.lines ? state.lines.size : 0)) saveLines();
      }
      App.map.setVisibleRoutes(state.lines);
      App.map.drawRoutes(routes);
      renderLinesMenu();
      handlers.onVehicles(vehicles);
      alerts.forEach((a) => state.alerts.set(a.alert_id, a));
    } catch (e) {
      console.error("Не удалось загрузить начальные данные:", e);
      state.status = "degraded";
      renderStatus();
    }

    source.getMetrics()
      .then((m) => {
        if (!m || typeof m !== "object") return;
        // ML-сервис отдаёт mae_test_s / latency_ms_p50; поддерживаем и старые имена.
        // Filter выкидывает null/undefined/NaN — иначе в UI появляется "NaN с".
        const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
        const mae = num(m.mae_test_s) ?? num(m.mae_sec);
        const p95 = num(m.latency_ms_p95) ?? num(m.p95_latency_ms);
        const lat = p95 ?? num(m.latency_ms_p50);
        const latName = p95 != null ? "p95" : "p50";
        const parts = [];
        if (mae != null) parts.push(`MAE <b>${Math.round(mae)} с</b>`);
        if (lat != null) parts.push(`${latName} <b>${Math.round(lat)} мс</b>`);
        // Покрытие 80%-интервала (p10..p90 + conformal margin): доля фактов, попавших внутрь.
        // Ждём ~80% — если сильно ниже, интервал слишком узкий и «под риском» врёт.
        const cov = num(m?.uncertainty?.coverage_calibrated_holdout) ?? num(m?.uncertainty?.coverage_calibrated);
        if (cov != null) parts.push(`покрытие <b>${Math.round(cov * 100)}%</b>`);
        if (parts.length) $("model-info").innerHTML = parts.join(" · ");
        renderLeadBars(m?.validation?.mae_by_lead);
      })
      .catch(() => {});

    renderList();
    renderRoutes();
    renderHourlyHeatmap();
    renderWorstStops();
    renderBunching();
    source.start(handlers);

    setInterval(renderRoutes, 2000);            // светофор маршрутов
    setInterval(renderHorizon, 1000);           // шкала «ближайшие 15 минут»
    renderVerified();
    setInterval(refreshSchedule, 2000);         // расписание выбранного ТС
    setInterval(() => !state.selectedId && renderList(), 15000); // «через N мин» в списке
    setInterval(() => state.status === "degraded" && renderStatus(), 5000);
    setInterval(renderHourlyHeatmap, 60_000);   // подсветка «текущего часа» раз в минуту
    setInterval(renderWorstStops, 10_000);      // проблемные остановки — быстро реагируем на события
    setInterval(renderBunching, 10_000);        // слипания «паровозиком» — тоже событийная штука
  }

  App.state = state; // для отладки в консоли браузера
  document.addEventListener("DOMContentLoaded", init);
})(window.App);
