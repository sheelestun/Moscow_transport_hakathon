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
  App.routeLabel = (routeId) => {
    const r = state.routes.get(routeId);
    return (r && r.route_number) || routeId;
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
            <span class="muted">${App.esc(App.routeLabel(s.route_id))} · ${s.vehicles} ТС · пик ${Math.round(s.max_delay_sec)}с</span>
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
        el.innerHTML = "";
        const blk = $("bunching-block"); if (blk) blk.hidden = true;
        App.map.setBunchingPairs && App.map.setBunchingPairs([]);
        return;
      }
      const blk = $("bunching-block"); if (blk) blk.hidden = false;
      el.innerHTML = pairs.slice(0, 6).map((p) => {
        const min = (sec) => Math.max(1, Math.round(sec / 60));
        return `<li><button class="worst-stop worst-stop--btn" data-vid="${App.esc(p.follower_id)}" title="Открыть догоняющий автобус">
          <span class="worst-stop__delay t-red">${min(p.headway_sec)} мин</span>
          <span class="worst-stop__body">
            <b>${App.esc(App.routeLabel ? App.routeLabel(p.route_id) : p.route_id)}: ТС ${App.esc(p.follower_id)} догоняет ТС ${App.esc(p.leader_id)}</b>
            <span class="muted">между ними ${min(p.headway_sec)} мин, по плану ${min(p.plan_headway_sec)} мин</span>
          </span>
        </button></li>`;
      }).join("");
      el.querySelectorAll("[data-vid]").forEach((b) => (b.onclick = () => selectVehicle(b.dataset.vid)));
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
      canApply: !!source.applyMeasure, // «Применить» есть только в демо-симуляции
      appliedFor: () => activeMeasures(state.vehicles.get(id).route_id, id),
      onRecEffect: (scenario) => {
        const cur = state.vehicles.get(id);
        const a = alertFor(id);
        return source.whatif({ scenario, route_id: cur.route_id, vehicle_id: id, at_stop_id: a ? a.target_stop_id : null });
      },
      onApply: (scenario) => applyMeasure(scenario, state.vehicles.get(id).route_id, id),
      onWhatifOpen: () => {
        const cur = state.vehicles.get(id);
        const a = alertFor(id);
        App.whatif.open({
          source,
          vehicle: cur,
          route: state.routes.get(cur.route_id),
          alert: a,
          rec: App.toScenario((a && a.recommendation) || cur.recommendation),
          applied: activeMeasures(cur.route_id, id),
          onApply: (scenario) => applyMeasure(scenario, cur.route_id, id),
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
          // в live-режиме getSignals асинхронный и светофоров нет — пропускаем
          const sig = source.getSignals(r.route_id);
          if (Array.isArray(sig)) sig.forEach((s) => list.push({ ...s, sel }));
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

  // ---------- Полоса «Через 10–15 минут»: столбики, кто сильнее опоздает ----------
  // Слева направо — от самого большого прогноза опоздания к меньшему. Высота столбика — опоздание.
  // Берём все ТС видимых линий с прогнозом опоздания от 1 минуты (жёлтые и красные).
  const HZ_MIN_DELAY = 60;
  function renderHorizon() {
    const track = $("horizon");
    const vs = visibleVehicles()
      .filter((v) => v.delay_pred_sec >= HZ_MIN_DELAY)
      .sort((a, b) => b.delay_pred_sec - a.delay_pred_sec);
    const red = vs.filter((v) => App.riskLevel(v.risk_score) === "red").length;
    $("horizon-count").textContent = red ? `${red} ${plural(red, "опоздает", "опоздают", "опоздают")} сильно` : "";
    // Строка для свёрнутого вида
    const top = vs[0];
    $("horizon-summary").innerHTML = !vs.length
      ? `<span class="t-green">все по графику</span>`
      : `<b class="t-${App.riskLevel(top.risk_score)}">${App.esc(App.routeLabel(top.route_id))} · ТС ${App.esc(top.vehicle_id)} ${App.fmtDelayShort(top.delay_pred_sec)}</b>` +
        (vs.length > 1 ? ` · ещё ${vs.length - 1} с опозданием` : "");
    if (document.body.classList.contains("hz-collapsed")) return;

    if (!vs.length) {
      track.innerHTML = `<div class="hzb-empty">Все по графику — опозданий через 10–15 минут не ожидается</div>`;
      return;
    }
    // сколько столбиков влезает по ширине
    const fit = Math.max(3, Math.floor(track.clientWidth / 92));
    const shown = vs.slice(0, fit);
    const max = Math.max(600, shown[0].delay_pred_sec); // шкала — минимум до 10 минут
    track.innerHTML = `<div class="hzb">${shown.map((v) => {
      const level = App.riskLevel(v.risk_score);
      const h = Math.max(6, Math.round((v.delay_pred_sec / max) * 100));
      return `<button class="hzb__col hzb__col--${level} ${v.vehicle_id === state.selectedId ? "is-selected" : ""}" data-vid="${App.esc(v.vehicle_id)}"
          title="${App.esc(App.routeLabel(v.route_id))}, ТС ${App.esc(v.vehicle_id)}: через 10–15 мин опоздает на ${App.fmtDelayShort(v.delay_pred_sec)} (сейчас ${App.fmtDelayShort(v.delay_now_sec)})">
          <span class="hzb__val">${App.fmtDelayShort(v.delay_pred_sec)}</span>
          <span class="hzb__bar"><i style="height:${h}%"></i></span>
          <span class="hzb__lab"><b>${App.esc(App.routeLabel(v.route_id))}</b> ${App.esc(String(v.vehicle_id).slice(-4))}</span>
        </button>`;
    }).join("")}${vs.length > shown.length ? `<span class="hzb__more">ещё ${vs.length - shown.length}</span>` : ""}</div>`;
    track.querySelectorAll("[data-vid]").forEach((b) => (b.onclick = () => selectVehicle(b.dataset.vid)));
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
      : "Прогноз против факта";
    $("verified").innerHTML = list.slice(0, 5).map((v) => {
      const err = v.delay_fact_sec - v.delay_pred_sec;
      let mark, cls;
      if (v.measure && v.delay_fact_sec < v.delay_pred_sec - 30) { mark = "мера помогла"; cls = "ok"; }
      else if (Math.abs(err) <= HIT_SEC) { mark = "сбылся"; cls = "ok"; }
      else { mark = "ошибка " + App.fmtDelayShort(Math.abs(err)).replace(/^[+−]/, ""); cls = "miss"; }
      return `
        <li class="vf vf--${cls}">
          <span class="route-chip">${App.esc(App.routeLabel(v.route_id))}</span>
          <span class="vf__stop">«${App.esc(v.target_stop_name || v.target_stop_id)}»</span>
          <span class="vf__nums">прогноз <b>${App.fmtDelayShort(v.delay_pred_sec)}</b> · факт <b>${App.fmtDelayShort(v.delay_fact_sec)}</b></span>
          <span class="vf__mark">${cls === "ok" ? "✓" : "✗"} ${mark}</span>
        </li>`;
    }).join("") || `<li class="empty empty--muted">Пока нечего сверять</li>`;
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
        <span class="route-chip" data-type="${App.esc(r.transport_type || "")}">${App.esc(App.routeLabel(r.route_id))}</span>
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
  const APPLIED_TEXT = {
    signal_priority: (r) => `Приоритет на светофорах, маршрут ${r}, 20 мин`,
    hold_at_stop: () => `Догоняющий автобус придержан`,
    short_turn: () => `Автобус развёрнут раньше конечной`,
    express: () => `Автобус пущен экспрессом`,
    detour: () => `Автобус пущен в объезд`,
  };
  async function applyMeasure(scenario, routeId, vehicleId) {
    await source.applyMeasure({ scenario, route_id: routeId, vehicle_id: vehicleId });
    state.applied.push({ scenario, route_id: routeId, vehicle_id: vehicleId, at: App.now() });
    toast(APPLIED_TEXT[scenario] ? APPLIED_TEXT[scenario](routeId)
      : `Применено: ${App.labels.scenarios[scenario]}, маршрут ${routeId}`);
    renderVehicle();
  }

  // Меры, которые сейчас действуют (20 минут по часам симуляции): маршрутные — для всего маршрута,
  // «точечные» (развернуть, экспресс, объезд, придержать) — только для того ТС, к которому применили.
  // Мер можно применить несколько — эффекты складываются.
  const VEHICLE_MEASURES = new Set(["short_turn", "express", "detour", "hold_at_stop"]);
  function activeMeasures(routeId, vehicleId) {
    return state.applied.filter((x) => x.route_id === routeId && App.now() - x.at < 20 * 60000 &&
      (!VEHICLE_MEASURES.has(x.scenario) || x.vehicle_id === vehicleId));
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

    // Ссылка с mowtransit.ru (?vehicle=<id>): сразу открываем карточку этого ТС.
    // ТС может появиться не в первой пачке — ждём до 15 секунд.
    const deepId = new URLSearchParams(location.search).get("vehicle");
    if (deepId) {
      let tries = 0;
      const openDeep = () => {
        if (state.vehicles.has(deepId)) selectVehicle(deepId);
        else if (++tries < 15) setTimeout(openDeep, 1000);
        else toast(`ТС ${deepId} сейчас не в эфире`);
      };
      openDeep();
    }

    // Бейдж «симуляция ×N»: датасет проигрывается ускоренно, из-за чего маркеры на карте
    // едут быстрее реального — без индикатора это выглядит как баг («слишком быстро»).
    // Обновляем раз в 30 сек: скорость меняется только через рестарт бэкенда с новым CLOCK_SPEED.
    const refreshSimBadge = () => source.getHealth && source.getHealth().then((h) => {
      const badge = $("sim-badge");
      if (!badge) return;
      const sp = h && h.clock && h.clock.speed;
      if (!sp || Math.abs(sp - 1) < 0.05) { badge.hidden = true; return; }
      badge.hidden = false;
      badge.textContent = `×${Number.isInteger(sp) ? sp : sp.toFixed(1)} симуляция`;
    }).catch(() => {});
    refreshSimBadge();
    setInterval(refreshSimBadge, 30_000);

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
