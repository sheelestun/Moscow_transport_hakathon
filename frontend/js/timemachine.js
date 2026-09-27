// Машина времени: дашборд записывает, что было на линиях, и позволяет «отмотать» назад.
//
// Как устроено:
//  • Хранилище (App.createTimeline) — кадры «как всё выглядело» каждые 5 секунд за последний час:
//    ТС (позиции, прогнозы, риск) и какие алерты были активны. Плюс словарь всех алертов
//    и сверенные прогнозы — чтобы в прошлом показать, чем закончилось.
//  • Кадры приходят из двух мест: предыстория от источника данных (source.getHistory —
//    мок проматывает 40 минут, бэкенд отдаёт GET /history/frames) и запись в реальном времени.
//  • Панель (App.timeBar) — ползунок под картой: перемотка, проигрывание, «Вернуться в эфир».
//    Что именно показывать в прошлом, решает app.js.
window.App = window.App || {};

(function (App) {
  const FRAME_MS = 5000;        // кадр раз в 5 секунд (по часам симуляции / сервера)
  const KEEP_MS = 60 * 60000;   // храним последний час

  const ms = (t) => (typeof t === "number" ? t : new Date(t).getTime());

  App.createTimeline = function () {
    const frames = [];            // [{t, vehicles: [...], alertIds: [...]}] по возрастанию t
    const alerts = new Map();     // alert_id -> алерт (как пришёл)
    const outcomes = new Map();   // alert_id -> сверка {delay_pred_sec, delay_fact_sec, _at}

    function push(frame) {
      const last = frames[frames.length - 1];
      if (last && frame.t <= last.t) return;
      frames.push(frame);
      const cut = frame.t - KEEP_MS;
      while (frames.length && frames[0].t < cut) frames.shift();
    }

    return {
      FRAME_MS,
      // Записать текущее состояние (вызывается на каждом обновлении, кадры прореживаются сами)
      record(t, vehicles, alertIds) {
        const last = frames[frames.length - 1];
        if (last && t - last.t < FRAME_MS) return false;
        push({ t, vehicles: [...vehicles], alertIds: [...alertIds] });
        return true;
      },
      addAlert(a) { alerts.set(a.alert_id, a); },
      addOutcome(v) { outcomes.set(v.alert_id, v); },
      alert(id) { return alerts.get(id); },
      outcome(id) { return outcomes.get(id); },

      // Предыстория от источника данных. Формат одинаковый у мока и бэкенда:
      // {frames: [{t, vehicles, alert_ids}], alerts: [...], verified: [...]}
      seed(h) {
        if (!h) return;
        (h.alerts || []).forEach((a) => alerts.set(a.alert_id, a));
        (h.verified || []).forEach((v) => outcomes.set(v.alert_id, { ...v, _at: ms(v.verified_at) }));
        const old = (h.frames || [])
          .map((f) => ({ t: ms(f.t), vehicles: f.vehicles || [], alertIds: f.alert_ids || f.alertIds || [] }))
          .filter((f) => Number.isFinite(f.t))
          .sort((a, b) => a.t - b.t);
        // предыстория идёт раньше уже записанного
        const firstLive = frames.length ? frames[0].t : Infinity;
        frames.unshift(...old.filter((f) => f.t < firstLive));
      },

      range() {
        return frames.length ? { from: frames[0].t, to: frames[frames.length - 1].t } : null;
      },

      // Последний кадр не позже t
      frameAt(t) {
        let lo = 0, hi = frames.length - 1, best = null;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (frames[mid].t <= t) { best = frames[mid]; lo = mid + 1; }
          else hi = mid - 1;
        }
        return best || frames[0] || null;
      },

      // Сводка для полосы под ползунком: сколько ТС было «красными» и «жёлтыми» в каждом отрезке
      buckets(n) {
        const r = this.range();
        if (!r || r.to <= r.from) return [];
        const size = (r.to - r.from) / n;
        const out = Array.from({ length: n }, () => ({ red: 0, yellow: 0, total: 0 }));
        for (const f of frames) {
          const b = out[Math.min(n - 1, Math.floor((f.t - r.from) / size))];
          let red = 0, yellow = 0;
          for (const v of f.vehicles) {
            const l = App.riskLevel(v.risk_score);
            if (l === "red") red++; else if (l === "yellow") yellow++;
          }
          // в отрезке показываем «худший момент»
          if (red > b.red || (red === b.red && yellow > b.yellow)) { b.red = red; b.yellow = yellow; }
          b.total = Math.max(b.total, f.vehicles.length);
        }
        return out;
      },

      // Моменты появления алертов — отметки на шкале
      alertMarks() {
        const r = this.range();
        if (!r) return [];
        return [...alerts.values()]
          .map((a) => ({ a, t: ms(a.created_at) }))
          .filter((x) => x.t >= r.from && x.t <= r.to);
      },
    };
  };

  // ================= Панель под картой =================
  const $ = (id) => document.getElementById(id);
  const SPEEDS = [5, 20, 60];

  App.timeBar = {
    // ctx: { timeline, now(), onSeek(t) — показать момент t, onLive() — вернуться в эфир, onOpen(open) }
    init(ctx) {
      this.ctx = ctx;
      this.open = false;
      this.past = null;       // null = эфир, иначе время (мс), которое показываем
      this.playing = false;
      this.speed = 20;

      const slider = $("tm-slider");
      $("tm-btn").onclick = () => this.setOpen(!this.open);
      $("tm-close").onclick = () => { this.setOpen(false); };
      $("tm-live").onclick = () => this.goLive();
      $("tm-banner-live").onclick = () => this.goLive();
      $("tm-play").onclick = () => this.togglePlay();
      $("tm-back").onclick = () => this.jump(-5 * 60000);
      $("tm-fwd").onclick = () => this.jump(5 * 60000);
      $("tm-speed").innerHTML = SPEEDS.map((x) => `<button data-x="${x}">×${x}</button>`).join("");
      $("tm-speed").querySelectorAll("button").forEach((b) => (b.onclick = () => { this.speed = +b.dataset.x; this.renderSpeed(); }));
      this.renderSpeed();

      // Перетаскивание ползунка: показываем кадр сразу, без анимации
      slider.oninput = () => {
        const r = ctx.timeline.range();
        if (!r) return;
        const t = r.from + (+slider.value / 1000) * (ctx.now() - r.from);
        this.pause();
        if (+slider.value >= 1000) this.goLive();
        else this.seek(t, { instant: true });
      };
      // Клавиши, пока панель открыта: ←/→ — на 30 с, Пробел — пауза
      document.addEventListener("keydown", (e) => {
        if (!this.open || e.target.closest("input[type=search], input[type=text], textarea")) return;
        if (e.key === "ArrowLeft") { e.preventDefault(); this.jump(-30000); }
        else if (e.key === "ArrowRight") { e.preventDefault(); this.jump(30000); }
        else if (e.key === " " && e.target === document.body) { e.preventDefault(); this.togglePlay(); }
      });

      setInterval(() => this.renderStrip(), 5000);
      setInterval(() => this.render(), 1000);
      this.render();
    },

    setOpen(open) {
      this.open = open;
      $("timebar").hidden = !open;
      $("tm-btn").classList.toggle("is-on", open);
      $("tm-btn").setAttribute("aria-expanded", String(open));
      document.body.classList.toggle("tm-open", open);
      if (!open) this.goLive();
      else { this.renderStrip(); this.render(); }
      this.ctx.onOpen && this.ctx.onOpen(open);
    },

    isPast() { return this.past != null; },

    seek(t, opts = {}) {
      const r = this.ctx.timeline.range();
      if (!r) return;
      if (t >= this.ctx.now() - 1000) { this.goLive(); return; }
      this.past = Math.max(r.from, t);
      if (!this.open) this.setOpen(true);
      this.ctx.onSeek(this.past, opts);
      this.render();
    },

    jump(dt) {
      const cur = this.past != null ? this.past : this.ctx.now();
      this.seek(cur + dt, { duration: 300 });
    },

    goLive() {
      this.pause();
      if (this.past == null) { this.render(); return; }
      this.past = null;
      this.ctx.onLive();
      this.render();
    },

    togglePlay() {
      if (this.playing) return this.pause();
      if (this.past == null) {
        // из эфира «Play» проигрывает последние 10 минут
        const r = this.ctx.timeline.range();
        if (!r) return;
        this.seek(Math.max(r.from, this.ctx.now() - 10 * 60000), { instant: true });
      }
      this.playing = true;
      let lastReal = performance.now();
      this._timer = setInterval(() => {
        const nowReal = performance.now();
        const dt = (nowReal - lastReal) * this.speed;
        lastReal = nowReal;
        if (this.past == null) return this.pause();
        this.seek(this.past + dt, { duration: 260 });
      }, 250);
      this.render();
    },

    pause() {
      this.playing = false;
      clearInterval(this._timer);
      $("tm-play").textContent = "▶";
      $("tm-play").title = "Проиграть";
    },

    renderSpeed() {
      $("tm-speed").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.x === this.speed)));
    },

    // Полоса «как было»: высота — сколько ТС опаздывали, отметки — когда появлялись алерты
    renderStrip() {
      if (!this.open) return;
      const tl = this.ctx.timeline;
      const r = tl.range();
      const strip = $("tm-strip");
      if (!r) { strip.innerHTML = ""; return; }
      const bs = tl.buckets(90);
      const max = Math.max(1, ...bs.map((b) => b.red + b.yellow));
      const bars = bs.map((b) => {
        const hr = (b.red / max) * 100, hy = (b.yellow / max) * 100;
        return `<i style="--r:${hr.toFixed(1)}%;--y:${hy.toFixed(1)}%"></i>`;
      }).join("");
      const marks = tl.alertMarks().map(({ a, t }) =>
        `<b class="tm-mark" style="left:${(((t - r.from) / (r.to - r.from)) * 100).toFixed(2)}%" title="${App.fmtTime(new Date(t))} · алерт: маршрут ${App.esc(a.route_id)}, ${App.fmtDelayShort(a.delay_pred_sec)} к «${App.esc(a.target_stop_name || a.target_stop_id)}»"></b>`).join("");
      strip.innerHTML = `<div class="tm-bars">${bars}</div>${marks}`;
      $("tm-from").textContent = App.fmtTime(new Date(r.from));
    },

    render() {
      const tl = this.ctx.timeline;
      const r = tl.range();
      const past = this.past != null;
      document.body.classList.toggle("is-past", past);
      $("tm-banner").hidden = !past;
      $("tm-live").hidden = !past;
      $("tm-playing").hidden = !this.playing;
      if (this.playing) { $("tm-play").textContent = "❚❚"; $("tm-play").title = "Пауза"; }
      if (!r) return;
      const now = this.ctx.now();
      const t = past ? this.past : now;
      const span = Math.max(1, now - r.from);
      const pos = past ? Math.round(((t - r.from) / span) * 1000) : 1000;
      const slider = $("tm-slider");
      if (document.activeElement !== slider || !past) slider.value = pos;
      $("tm-fill").style.width = pos / 10 + "%";
      const ago = Math.round((now - t) / 60000);
      $("tm-time").textContent = App.fmtTime(new Date(t), true);
      $("tm-ago").textContent = past ? (ago <= 0 ? "меньше минуты назад" : `${ago} мин назад`) : "сейчас · в эфире";
      $("tm-banner-time").textContent = App.fmtTime(new Date(t), true);
      $("tm-banner-ago").textContent = ago <= 0 ? "меньше минуты назад" : `${ago} мин назад`;
      const mins = Math.round((now - r.from) / 60000);
      $("tm-depth").textContent = `записано ${mins} мин`;
    },
  };
})(window.App);
