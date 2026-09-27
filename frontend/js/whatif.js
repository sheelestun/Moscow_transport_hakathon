// Окно What-if: сравнение всех мер для маршрута и кнопка «Применить».
// Открывается из карточки автобуса кнопкой «Что если…».
window.App = window.App || {};

(function (App) {
  const $ = (id) => document.getElementById(id);
  let ctx = null;       // {source, vehicle, route, alert, rec, onApplied}
  let results = [];     // [{scenario, res}]
  let chosen = new Set(); // выбранные меры (можно несколько)
  let lastFocus = null;

  App.whatif = {
    isOpen: () => !$("whatif-modal").hidden,

    async open(c) {
      ctx = c;
      results = [];
      chosen = new Set();
      lastFocus = document.activeElement;
      const modal = $("whatif-modal");
      modal.hidden = false;
      $("wi-title").textContent = `Сравнение мер · маршрут ${c.vehicle.route_id}`;
      $("wi-sub").textContent = c.route ? c.route.name : "";
      $("wi-body").innerHTML = `<div class="wi-loading">Модель пересчитывает прогноз для каждой меры…</div>`;
      $("wi-apply").hidden = true;
      $("wi-close").focus();

      // Считаем все меры параллельно
      // Какие меры умеет считать источник (у бэкенда пока 5 базовых), остальные просто не показываем
      const codes = c.source.scenarios || Object.keys(App.labels.scenarios);
      try {
        const all = (await Promise.all(codes.map((scenario) =>
          c.source.whatif({ scenario, route_id: c.vehicle.route_id, vehicle_id: c.vehicle.vehicle_id, at_stop_id: c.alert ? c.alert.target_stop_id : null })
            .then((res) => ({ scenario, res }))
            .catch(() => null)))).filter(Boolean);
        if (ctx !== c) return; // окно успели закрыть
        if (!all.length) throw new Error("ни одна мера не посчиталась");
        results = all;
        const best = bestOf(all);
        const recDone = (c.applied || []).some((x) => x.scenario === c.rec);
        chosen = new Set([!recDone && all.some((r) => r.scenario === c.rec) ? c.rec : best]);
        render();
      } catch (e) {
        $("wi-body").innerHTML = `<p class="error">Не удалось рассчитать: ${App.esc(e.message)}. Проверьте связь с сервером и откройте окно ещё раз.</p>`;
      }
    },

    close() {
      $("whatif-modal").hidden = true;
      ctx = null;
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    },
  };

  // Лучшая мера — та, после которой меньше всего опаздывающих, при равенстве — меньше среднее опоздание
  function bestOf(all) {
    const fresh = all.filter((r) => !(ctx && ctx.applied || []).some((x) => x.scenario === r.scenario));
    return (fresh.length ? fresh : all).slice().sort((a, b) =>
      (a.res.summary.red_after - b.res.summary.red_after) ||
      (App.measureEffect(b.res, ctx.vehicle.vehicle_id).gain - App.measureEffect(a.res, ctx.vehicle.vehicle_id).gain))[0].scenario;
  }

  function render() {
    const best = bestOf(results);
    const s0 = results[0].res.summary;
    const total = results[0].res.vehicles.length;
    const vid = ctx.vehicle.vehicle_id;
    const maxGain = Math.max(1, ...results.map((r) => App.measureEffect(r.res, vid).gain));

    const rows = results
      .slice()
      .sort((a, b) => (a.scenario === best ? -1 : b.scenario === best ? 1 : 0) || App.measureEffect(b.res, vid).gain - App.measureEffect(a.res, vid).gain)
      .map(({ scenario, res }) => {
        const s = res.summary;
        const gain = App.measureEffect(res, vid).gain; // что мера даст ЭТОМУ автобусу
        const done = (ctx.applied || []).some((x) => x.scenario === scenario);
        const tags = done ? `<span class="tag">уже применено</span>` : scenario === best && scenario === ctx.rec
          ? `<span class="tag tag--best">лучший · советует модель</span>`
          : (scenario === best ? `<span class="tag tag--best">лучший</span>` : "") +
            (scenario === ctx.rec ? `<span class="tag">советует модель</span>` : "");
        return `
        <button class="wi-opt ${chosen.has(scenario) ? "is-chosen" : ""} ${done ? "is-done" : ""}" data-sc="${scenario}" role="checkbox" aria-checked="${chosen.has(scenario)}" ${done ? "disabled" : ""}>
          <span class="wi-opt__name"><i class="wi-check" aria-hidden="true">${chosen.has(scenario) || done ? "✓" : ""}</i>${App.esc(App.labels.scenarios[scenario])}${tags}<small class="wi-opt__scope">${App.esc(App.labels.scope[scenario] || "")}</small></span>
          <span class="wi-opt__gain ${done ? "muted" : gain > 0 ? "t-green" : gain < 0 ? "t-red" : "muted"}">${done ? "✓" : gain === 0 ? "не поможет" : (gain > 0 ? "−" : "+") + App.fmtDelayShort(Math.abs(gain)).replace(/^[+−]/, "")}</span>
          <span class="wi-opt__bar" title="Чем длиннее, тем сильнее эффект"><i style="width:${Math.max(2, (Math.max(0, gain) / maxGain) * 100)}%"></i></span>
          <span class="wi-opt__red">на маршруте опаздывают <b>${s.red_before} → ${s.red_after}</b></span>
        </button>`;
      }).join("");

    // Несколько мер сразу: эффекты складываются (для каждого автобуса суммируем выигрыши)
    const sel = results.filter((r) => chosen.has(r.scenario));
    const combo = combine(sel);
    const me = combo.vehicles.find((x) => x.vehicle_id === vid);
    const myGain = me ? me.delay_before_sec - me.delay_after_sec : 0;
    const names = sel.map((r) => `«${App.esc(App.labels.scenarios[r.scenario])}»`).join(" + ");
    $("wi-body").innerHTML = `
      <div class="wi-now">
        <div><span class="muted small">Сейчас на маршруте</span><b>${App.fmtDelayShort(s0.avg_delay_before_sec)}</b><span class="muted small">среднее опоздание</span></div>
        <div><span class="muted small">Опаздывают</span><b class="${s0.red_before ? "t-red" : ""}">${s0.red_before} из ${total}</b><span class="muted small">автобусов</span></div>
      </div>
      ${(ctx.applied || []).length ? `<p class="wi-note">Уже применено: ${ctx.applied.map((x) => App.esc(App.labels.scenarios[x.scenario])).join(", ")}. Цифры ниже — что добавит ещё мера поверх них.</p>` : ""}
      <h4>Отметьте одну или несколько мер</h4>
      <div class="wi-opts" role="group" aria-label="Меры">${rows}</div>
      ${sel.length ? `
      <div class="wi-combo">
        <div class="wi-combo__title">${sel.length > 1 ? `Вместе: ${names}` : names}</div>
        <div class="wi-combo__nums">
          <span>этому автобусу <b class="${myGain > 0 ? "t-green" : myGain < 0 ? "t-red" : ""}">${myGain === 0 ? "0" : (myGain > 0 ? "−" : "+") + App.fmtDelayShort(Math.abs(myGain)).replace(/^[+−]/, "")}</b></span>
          <span>опаздывают на маршруте <b>${combo.redBefore} → ${combo.redAfter}</b></span>
        </div>
      </div>
      ${sel.map((r) => r.res.note ? `<p class="wi-note">${App.esc(r.res.note)}</p>` : "").join("")}
      <h4>По автобусам</h4>
      ${vehiclesHtml(combo)}` : `<p class="muted">Ничего не выбрано.</p>`}`;

    $("wi-body").querySelectorAll("[data-sc]").forEach((b) => (b.onclick = () => {
      const sc = b.dataset.sc;
      chosen.has(sc) ? chosen.delete(sc) : chosen.add(sc);
      render();
    }));

    const apply = $("wi-apply");
    if (ctx.source.applyMeasure) {
      apply.hidden = false;
      apply.disabled = !sel.length;
      apply.textContent = sel.length > 1 ? `Применить ${sel.length} ${sel.length < 5 ? "меры" : "мер"}` : sel.length ? "Применить эту меру" : "Выберите меру";
      apply.onclick = async () => {
        apply.disabled = true;
        for (const r of sel) await ctx.onApply(r.scenario); // по очереди, эффекты складываются
        App.whatif.close();
      };
    }
  }

  // Суммарный эффект нескольких мер: для каждого ТС складываем выигрыши;
  // опаздывающему мера не делает «раньше графика» — ниже нуля не опускаем
  function combine(sel) {
    const base = results[0].res;
    const vehicles = base.vehicles.map((v0) => {
      let gain = 0;
      for (const r of sel) {
        const v = r.res.vehicles.find((x) => x.vehicle_id === v0.vehicle_id);
        if (v) gain += v.delay_before_sec - v.delay_after_sec;
      }
      let after = v0.delay_before_sec - gain;
      if (v0.delay_before_sec > 0 && after < 0) after = 0;
      return { vehicle_id: v0.vehicle_id, delay_before_sec: v0.delay_before_sec, delay_after_sec: Math.round(after) };
    });
    const red = (sec) => App.delayLevel(sec) === "red";
    return {
      vehicles,
      redBefore: base.summary.red_before,
      redAfter: sel.length ? vehicles.filter((v) => red(v.delay_after_sec)).length : base.summary.red_before,
    };
  }

  function vehiclesHtml(w) {
    const max = Math.max(60, ...w.vehicles.map((v) => Math.abs(v.delay_before_sec)));
    const bar = (sec) => `${Math.max(2, (Math.max(0, sec) / max) * 100)}%`;
    return `
      <div class="wi-veh">
        ${w.vehicles.map((v) => `
          <div class="wrow ${v.vehicle_id === ctx.vehicle.vehicle_id ? "wrow--me" : ""}">
            <span>ТС ${App.esc(v.vehicle_id)}</span>
            <span class="wrow__bars"><i class="b-before" style="width:${bar(v.delay_before_sec)}"></i><i class="b-after" style="width:${bar(v.delay_after_sec)}"></i></span>
            <span class="wrow__val">${App.fmtDelayShort(v.delay_before_sec)} → <b class="t-${App.delayLevel(v.delay_after_sec)}">${App.fmtDelayShort(v.delay_after_sec)}</b></span>
          </div>`).join("")}
        <div class="legend-inline muted small"><i class="b-before"></i>сейчас <i class="b-after"></i>после меры</div>
      </div>`;
  }

  // Закрытие: крестик, кнопка «Закрыть», клик по фону, Esc
  document.addEventListener("DOMContentLoaded", () => {
    $("wi-close").onclick = App.whatif.close;
    $("wi-cancel").onclick = App.whatif.close;
  });
})(window.App);
