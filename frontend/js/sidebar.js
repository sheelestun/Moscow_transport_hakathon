// Левая панель диспетчера: список алертов и маршрутов ИЛИ карточка выбранного ТС.
window.App = window.App || {};

(function (App) {
  const $ = (id) => document.getElementById(id);
  const t = (dict, code) => App.esc(App.labels.t(dict, code));

  App.sidebar = {
    // ================= Список =================
    renderAlerts(alerts, opts) {
      const { onSelect } = opts;
      // Подсказка про алерты на скрытых линиях — чтобы не пропустить проблему
      const h = opts.hidden || { count: 0 };
      $("alerts-hidden").hidden = !h.count;
      if (h.count) {
        $("alerts-hidden").innerHTML = `Ещё ${h.count} на скрытых линиях (${h.routes.map(App.esc).join(", ")}) · <button class="link" id="show-all-lines">показать все линии</button>`;
        $("show-all-lines").onclick = opts.onShowAll;
      }
      const ul = $("alerts");
      $("alerts-count").textContent = alerts.length;
      $("alerts-count").classList.toggle("badge--hot", alerts.length > 0);

      if (!alerts.length) {
        ul.innerHTML = opts.noLines
          ? `<li class="empty empty--muted">Линии не выбраны. Нажмите «Линии» на карте и отметьте нужные.</li>`
          : `<li class="empty">Всё по графику. Опозданий в ближайшие 15 минут не ожидается.</li>`;
        return;
      }
      ul.innerHTML = alerts.map((a) => {
        const level = App.riskLevel(a.risk_score);
        return `
        <li>
          <button class="alert alert--${level} ${a._fresh ? "is-fresh" : ""}" data-vid="${App.esc(a.vehicle_id)}">
            <span class="alert__delay">${App.fmtDelayShort(a.delay_pred_sec)}</span>
            <span class="alert__body">
              <span class="alert__top"><span class="route-chip">${App.esc(App.routeLabel(a.route_id))}</span> ТС ${App.esc(a.vehicle_id)}</span>
              <span class="alert__where">к «${App.esc(a.target_stop_name || a.target_stop_id)}» · ${App.fmtIn(a.eta_incident)}</span>
              <span class="alert__reason">${t("reasons", a.reason_pattern)}</span>
            </span>
            <span class="alert__risk">${App.pct(a.risk_score)}</span>
          </button>
        </li>`;
      }).join("");
      ul.querySelectorAll("[data-vid]").forEach((b) => (b.onclick = () => onSelect(b.dataset.vid)));
    },

    renderRoutes(stats, { onSelect }) {
      $("routes").innerHTML = stats.map((r) => `
        <li>
          <button class="route" data-id="${App.esc(r.route_id)}">
            <span class="dot dot--${r.level}"></span>
            <span class="route-chip">${App.esc(App.routeLabel(r.route_id))}</span>
            <span class="route__name">${App.esc(r.name)}</span>
            <span class="route__stat">${r.red ? `<b class="t-red">${r.red}</b>` : ""}${r.yellow ? `<b class="t-yellow">${r.yellow}</b>` : ""}<span>${r.total} ТС</span></span>
          </button>
        </li>`).join("");
      $("routes").querySelectorAll("[data-id]").forEach((b) => (b.onclick = () => onSelect(b.dataset.id)));
    },

    // ================= Карточка ТС =================
    openVehicle(ctx) {
      $("view-list").hidden = true;
      $("view-vehicle").hidden = false;
      $("view-vehicle").innerHTML = `
        <div class="vh__nav">
          <button class="link" id="vh-back">← Все ТС под риском</button>
        </div>
        <section id="vh-hero"></section>
        <section id="vh-why" class="block"></section>
        <section id="vh-act"></section>
        <section id="vh-sched" class="block"></section>`;
      $("vh-back").onclick = ctx.onBack;
      this._ctx = ctx;
      this._actKey = null;
      this._whyKey = null;
    },

    closeVehicle() {
      $("view-vehicle").hidden = true;
      $("view-vehicle").innerHTML = "";
      $("view-list").hidden = false;
      this._ctx = null;
    },

    // Вызывается каждую секунду: обновить цифры
    updateVehicle({ vehicle: v, route, alert, schedule, whatif, hours }) {
      if (!this._ctx || !v) return;
      const level = App.riskLevel(v.risk_score);
      const target = schedule && schedule.stops.find((s) => s.is_target);

      // --- Шапка ---
      const whereName = alert ? alert.target_stop_name || alert.target_stop_id : target && target.name;
      const whereTime = alert ? alert.eta_incident : target && (target.time_pred || target.time_plan);
      $("vh-hero").innerHTML = `
        <div class="hero hero--${level}">
          <div class="hero__top">
            <span class="route-chip route-chip--lg">${App.esc(App.routeLabel(v.route_id))}</span>
            <div class="hero__title">
              <b>ТС ${App.esc(v.vehicle_id)}</b>
              <span class="muted">${App.esc(schedule ? schedule.direction : route ? route.name : "")}</span>
            </div>
            <span class="level level--${level}">${App.levelName[level]}</span>
            ${sourceBadgeHtml(v.source)}
          </div>
          ${dataStatusHtml(v.data_status)}
          <div class="hero__label">Прогноз через 10–15 минут</div>
          <div class="hero__big">${App.fmtDelay(v.delay_pred_sec)}${intervalHtml(v.delay_interval_sec, v.delay_pred_sec)}</div>
          ${probabilitiesHtml(v)}
          ${whereName ? `<div class="hero__where">к остановке «${App.esc(whereName)}» · ${App.fmtTime(whereTime)} (${App.fmtIn(whereTime)})</div>` : ""}
          ${hoursHtml(hours)}
          ${problemHtml(schedule, level)}
          ${v.waiting_signal && v.waiting_signal.waited_sec >= 2 ? `<div class="waiting"><span class="sig-ico"><i class="r"></i><i class="g"></i></span>
            <span>Сейчас стоит на красном светофоре <b>${v.waiting_signal.waited_sec} с</b> · зелёный через ${v.waiting_signal.left_sec} с</span></div>` : ""}
          <div class="hero__grid">
            <div><span class="muted">Сейчас</span><b class="t-${App.delayLevel(v.delay_now_sec)}">${App.fmtDelayShort(v.delay_now_sec)}</b></div>
            <div><span class="muted">Скорость</span><b>${v.speed ?? "—"} км/ч</b></div>
            <div><span class="muted">Риск</span><b>${App.pct(v.risk_score)}</b></div>
            <div><span class="muted">Уверенность</span><b>${v.confidence != null ? App.pct(v.confidence) : alert && alert.confidence != null ? App.pct(alert.confidence) : "—"}</b></div>
            ${headwayHtml(v)}
          </div>
        </div>`;

      // --- Почему (перерисовываем только при смене причины) ---
      const reason = (alert && alert.reason_pattern) || v.reason_pattern;
      const feats = (alert && alert.top_features) || v.top_features || [];
      const whyKey = level === "green" ? "green" : reason + feats.map((f) => f.name).join();
      if (whyKey !== this._whyKey) {
        this._whyKey = whyKey;
        $("vh-why").innerHTML = level === "green"
          ? `<h4>Почему такой прогноз</h4><p class="why__ok">Идёт по графику. Модель не видит признаков опоздания в ближайшие 15 минут.</p>`
          : `<h4>Почему опаздывает</h4>
             <p class="why__title">${t("reasons", reason)}</p>
             <p class="why__text">${t("explanations", reason)}</p>
             ${feats.length ? `<div class="feats"><div class="muted small">Что сильнее всего повлияло на прогноз</div>${featBars(feats)}</div>` : ""}`;
      }

      // --- Что делать (перерисовываем только при смене уровня/алерта, чтобы не сбить выбор в What-if) ---
      const rec = App.toScenario((alert && alert.recommendation) || v.recommendation);
      const applied = this._ctx.isApplied(v.route_id);
      const actKey = level === "green" ? "green" : `${rec}|${alert ? alert.alert_id : ""}|${applied ? applied.at : ""}`;
      if (actKey !== this._actKey) {
        this._actKey = actKey;
        const ctx = this._ctx;
        if (level === "green") {
          $("vh-act").innerHTML = "";
        } else {
          $("vh-act").innerHTML = actHtml(rec, alert, null, applied, ctx.canApply);
          bindAct(ctx, rec);
          // Эффект рекомендованной меры считаем сразу — чтобы диспетчер видел пользу без лишних кликов
          if (App.labels.scenarios[rec] && !applied) {
            ctx.onRecEffect(rec).then((eff) => {
              if (this._actKey !== actKey) return;
              $("vh-act").innerHTML = actHtml(rec, alert, eff, applied, ctx.canApply);
              bindAct(ctx, rec);
            }).catch(() => {});
          }
        }
      }

      // --- Расписание ---
      $("vh-sched").innerHTML = schedule ? scheduleHtml(schedule) : `<h4>Расписание</h4><p class="muted">Загружаем…</p>`;
    },
  };

  // Откуда пришёл последний фикс: живая NDTP-телеметрия vs replay из датасета.
  // Мок ничего не шлёт → рендерим только когда source явно задан бэкендом.
  const SOURCE_BADGE = {
    ndtp: { cls: "live", text: "LIVE", title: "Живая NDTP-телеметрия" },
    replay: { cls: "replay", text: "REPLAY", title: "Проигрывание traffic.csv из датасета" },
  };
  function sourceBadgeHtml(src) {
    const b = SOURCE_BADGE[src];
    if (!b) return "";
    return `<span class="src-badge src-badge--${b.cls}" title="${b.title}">${b.text}</span>`;
  }

  // Часы работы маршрута из /routes/{id}/hours — план первого и последнего рейса.
  // Полезно ночью: сразу видно, что маршрут не «пропал», а просто уже кончился (или ещё не начался).
  function hoursHtml(h) {
    if (!h || !h.first_hhmm || !h.last_hhmm) return "";
    const nextIn = h.next_departure ? App.fmtIn(h.next_departure) : null;
    const suffix = nextIn ? ` · след. рейс ${nextIn}` : " · сегодня рейсов больше нет";
    return `<div class="hero__hours">Часы работы: <b>${App.esc(h.first_hhmm)}–${App.esc(h.last_hhmm)}</b>${suffix}</div>`;
  }

  // Бейдж качества данных: скрываем при "live" (тишина = ОК), выделяем прочие статусы
  const DATA_STATUS = {
    stale: { cls: "warn", text: "Данные устарели — прогноз держится на последнем известном состоянии" },
    off_route: { cls: "warn", text: "ТС ушло с трассы маршрута — прогноз возможно неточен" },
    no_telemetry: { cls: "bad", text: "Нет телеметрии — показано последнее состояние" },
    fallback: { cls: "bad", text: "Модель на fallback: baseline вместо полной ML" },
  };
  function dataStatusHtml(status) {
    const cfg = DATA_STATUS[status];
    if (!cfg) return "";
    return `<div class="hero__data-status hero__data-status--${cfg.cls}">${cfg.text}</div>`;
  }

  // 80% доверительный интервал прогноза: показываем в виде "±Xс (80%)" рядом с большой цифрой
  function intervalHtml(interval, mean) {
    if (!Array.isArray(interval) || interval.length !== 2) return "";
    const [lo, hi] = interval;
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return "";
    const margin = Math.round((hi - lo) / 2);
    if (margin <= 0) return "";
    return `<span class="hero__interval" title="80% доверительный интервал прогноза">±${margin}с (80%)</span>`;
  }

  // Интервал до предыдущего ТС того же маршрута (bus bunching, Daganzo 2009).
  // Если факт < 60% плана — «паровозик»: их надо разводить (hold_at_stop / adjust_interval).
  function headwayHtml(v) {
    if (!Number.isFinite(v.headway_prev_sec)) return "";
    const h = Math.max(0, Math.round(v.headway_prev_sec));
    const plan = Number.isFinite(v.plan_headway_sec) && v.plan_headway_sec > 0 ? Math.round(v.plan_headway_sec) : null;
    const ratio = plan ? h / plan : null;
    const cls = ratio != null && ratio < 0.6 ? "t-red" : ratio != null && ratio < 0.85 ? "t-yellow" : "";
    const min = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    const title = plan ? `интервал до предыдущего ТС · план ${min(plan)}` : "интервал до предыдущего ТС";
    return `<div title="${title}"><span class="muted">Интервал</span><b class="${cls}">${min(h)}${plan ? ` <span class="muted small">/ ${min(plan)}</span>` : ""}</b></div>`;
  }

  // Вероятности исходов: раньше / в срок / позже
  function probabilitiesHtml(v) {
    const parts = [
      { key: "p_early", label: "Раньше", cls: "early" },
      { key: "p_ontime", label: "В срок", cls: "ontime" },
      { key: "p_late", label: "Позже", cls: "late" },
    ];
    const hasAny = parts.some((p) => Number.isFinite(v[p.key]));
    if (!hasAny) return "";
    return `<div class="hero__probs">
      ${parts.map((p) => `<span class="prob prob--${p.cls}"><i>${App.pct(v[p.key] || 0)}</i><em>${p.label}</em></span>`).join("")}
    </div>`;
  }

  // Проблемный участок: перегон до целевой остановки, где опоздание растёт сильнее всего
  function problemSegment(sch) {
    if (!sch) return null;
    const st = sch.stops;
    const targetIdx = st.findIndex((s) => s.is_target);
    const end = targetIdx >= 0 ? targetIdx : st.length - 1;
    let best = null;
    for (let i = 1; i <= end; i++) {
      const a = st[i - 1], b = st[i];
      if (b.status === "passed") continue; // уже проехали
      const grow = (b.delay_sec || 0) - (a.delay_sec || 0);
      if (!best || grow > best.grow) best = { from: a.name, to: b.name, grow, delay: b.delay_sec };
    }
    return best && best.grow >= 20 ? best : null;
  }

  function problemHtml(sch, level) {
    if (level === "green") return "";
    const p = problemSegment(sch);
    if (!p) return "";
    return `
      <div class="problem">
        <span class="problem__label">Проблемный участок</span>
        <b>${App.esc(p.from)} → ${App.esc(p.to)}</b>
        <span class="problem__grow">опоздание вырастет на ${App.fmtDelayShort(p.grow)} за перегон</span>
      </div>`;
  }

  function featBars(feats) {
    const list = feats.slice().sort((a, b) => b.contribution - a.contribution);
    const max = Math.max(...list.map((f) => f.contribution), 0.01);
    return list.map((f) => `
      <div class="feat">
        <span>${t("features", f.name)}</span>
        <span class="feat__bar"><i style="width:${(f.contribution / max) * 100}%"></i></span>
        <span class="feat__val">${Math.round(f.contribution * 100)}%</span>
      </div>`).join("");
  }

  // Блок «Рекомендация»: мера, её эффект и кнопка «Применить»
  function actHtml(rec, alert, eff, applied, canApply) {
    const known = !!App.labels.scenarios[rec];
    let effect = "";
    if (applied) {
      effect = `<div class="rec__done">✓ Применено в ${App.fmtTime(new Date(applied.at))}: ${App.esc(App.labels.scenarios[applied.scenario])}. Опоздания на маршруте отыгрываются — следите за картой.</div>`;
    } else if (known && !eff) {
      effect = `<div class="rec__effect rec__effect--loading">Считаем эффект…</div>`;
    } else if (eff) {
      const s = eff.summary;
      const gain = s.avg_delay_before_sec - s.avg_delay_after_sec;
      effect = `
        <div class="rec__effect">
          <div>
            <span class="rec__num ${gain > 0 ? "t-green" : "t-red"}">${gain > 0 ? "−" : "+"}${App.fmtDelayShort(Math.abs(gain)).replace(/^[+−]/, "")}</span>
            <span class="rec__cap">среднее опоздание<br>на маршруте</span>
          </div>
          <div>
            <span class="rec__num">${s.red_before} → <span class="${s.red_after < s.red_before ? "t-green" : ""}">${s.red_after}</span></span>
            <span class="rec__cap">опаздывающих<br>автобусов</span>
          </div>
        </div>`;
    }
    return `
      <div class="block block--rec">
        <h4>Рекомендация</h4>
        <p class="rec">${t("recommendations", rec)}</p>
        ${effect}
        <div class="row">
          ${known && canApply && !applied ? `<button class="btn" id="apply-btn">Применить</button>` : ""}
          <button class="btn btn--ghost" id="whatif-btn">Другие меры</button>
        </div>
        ${alert ? `<button class="link link--muted" id="ack-btn">Скрыть алерт — уже знаю</button>` : ""}
      </div>`;
  }

  function bindAct(ctx, rec) {
    const apply = $("apply-btn");
    if (apply) apply.onclick = async () => { apply.disabled = true; apply.textContent = "Применяем…"; await ctx.onApply(rec); };
    const btn = $("whatif-btn");
    if (btn) btn.onclick = () => ctx.onWhatifOpen();
    const ack = $("ack-btn");
    if (ack) ack.onclick = ctx.onAck;
  }

  // Длинные маршруты (50+ остановок): по умолчанию показываем «окно» — 2 пройденные, следующие до цели прогноза и 2 после
  let schedAll = false;
  document.addEventListener("click", (e) => {
    if (e.target && e.target.id === "sched-toggle") { schedAll = !schedAll; e.target.textContent = "…"; }
  });

  function scheduleHtml(sch) {
    const all = sch.stops;
    const iNext = Math.max(0, all.findIndex((s) => s.status !== "passed"));
    const iTarget = all.findIndex((s) => s.is_target);
    const from = schedAll ? 0 : Math.max(0, iNext - 2);
    const to = schedAll ? all.length - 1 : Math.min(all.length - 1, Math.max(iTarget, iNext) + 2);
    const hiddenBefore = from, hiddenAfter = all.length - 1 - to;
    const rows = all.slice(from, to + 1).map((s) => {
      const lvl = App.delayLevel(s.delay_sec);
      const real = s.status === "passed" ? s.time_fact : s.time_pred;
      const tag = s.is_target ? `<span class="st__tag">цель прогноза</span>` : s.status === "next" ? `<span class="st__tag st__tag--next">следующая</span>` : "";
      return `
        <li class="st st--${s.status} ${s.is_target ? "st--target" : ""}">
          <span class="st__rail"><i class="st__dot dot--${s.status === "passed" ? "passed" : lvl}"></i></span>
          <span class="st__name">${App.esc(s.name)}${tag}</span>
          <span class="st__time">${App.fmtTime(s.time_plan)}</span>
          <span class="st__time ${s.status === "passed" ? "" : "st__time--pred"}">${real ? App.fmtTime(real) : "—"}</span>
          <span class="st__dev dev--${s.status === "passed" ? "passed" : lvl}">${App.fmtDelayShort(s.delay_sec)}</span>
        </li>`;
    }).join("");
    const more = (n, where) => n ? `<li class="st st--more"><span class="st__rail"></span><span class="muted">… ещё ${n} ${where}</span></li>` : "";
    return `
      <h4>Расписание рейса · ${all.length} остановок</h4>
      <div class="sched__head"><span></span><span>Остановка</span><span>План</span><span>Факт/прогноз</span><span>Откл.</span></div>
      <ol class="sched">${more(hiddenBefore, "пройденных")}${rows}${more(hiddenAfter, "дальше по маршруту")}</ol>
      ${all.length > 8 ? `<button class="link" id="sched-toggle">${schedAll ? "Свернуть расписание" : "Показать всё расписание"}</button>` : ""}
      <p class="muted small">Прогноз времени для следующих остановок — от ML-модели. Прошедшие — фактическое время по телеметрии.</p>`;
  }
})(window.App);
