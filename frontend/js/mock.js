// Фейковый источник данных — имитирует бэкенд прямо в браузере.
// Отдаёт данные в ТОЧНО таком же формате, как настоящий бэкенд
// (контракт — frontend/README.md),
// поэтому остальной код не знает, мок это или живой API.
//
// Модель простая и честная: у каждого рейса есть плановое расписание
// (ТС должно ехать со средней скоростью PLAN_SPEED). Если ТС едет медленнее —
// копится опоздание. «Прогноз модели» экстраполирует тренд на 10–15 минут вперёд.
window.App = window.App || {};

(function (App) {
  const G = App.geo;
  const PLAN_SPEED = 18 / 3.6; // м/с — плановая средняя скорость с учётом остановок
  const HORIZON_S = 12.5 * 60; // середина окна прогноза 10–15 мин
  // Опаздывающий водитель едет чуть быстрее плана и понемногу отыгрывает (~1 мин за 10 мин пути):
  // в городе автобус сам почти не догоняет график — поэтому меры и нужны
  const CATCHUP_KMH = 20;

  const REASONS = ["traffic_jam_ahead", "long_dwell", "speed_drop", "bunching", "accumulated_delay"];
  // Меры — как у диспетчеров в Европе и США (обзор: Transport Reviews 2024, TCRP; приоритет — как iVRI/KAR в Нидерландах):
  //   signal_priority — условный приоритет: светофор продлевает зелёный / раньше включает его
  //                     только опаздывающему автобусу и только на несколько секунд;
  //   hold_at_stop    — придержать на остановке автобус, который ДОГОНЯЕТ опаздывающий (против «паровозика»);
  //   detour          — объезд, только если впереди затор/перекрытие;
  //   short_turn      — развернуть раньше конечной: обратный рейс начинается вовремя;
  //   express         — пустить экспрессом: проехать часть остановок без посадки;
  //   add_reserve / adjust_interval — выпустить резерв / выровнять интервалы на всём маршруте.
  const REC_FOR_REASON = {
    traffic_jam_ahead: "detour",
    long_dwell: "adjust_interval",
    speed_drop: "signal_priority",
    bunching: "hold_at_stop",
    accumulated_delay: "express",
  };
  // Эффект «маршрутных» мер в демо: какую долю опоздания мера снимает у опаздывающих ТС маршрута.
  // Скромно, как в жизни: резерв и выравнивание интервалов в первую очередь сокращают ожидание пассажиров,
  // а опоздавшему автобусу помогают косвенно (меньше людей на остановках — короче стоянки).
  const MEASURE_EFFECT = { add_reserve: 0.12, adjust_interval: 0.1 };
  const MEASURE_FITS = {
    add_reserve: ["accumulated_delay", "long_dwell"],
    adjust_interval: ["bunching", "long_dwell"],
    detour: ["traffic_jam_ahead"],
    signal_priority: ["speed_drop", "traffic_jam_ahead"],
    hold_at_stop: ["bunching"],
    short_turn: ["accumulated_delay", "traffic_jam_ahead"],
    express: ["accumulated_delay", "long_dwell"],
  };
  const TSP_EXTEND_S = 10;      // приоритет: зелёный продлевается максимум на 10 с…
  const TSP_EARLY_S = 15;       // …или включается раньше максимум на 15 с
  const TSP_LATE_S = 60;        // приоритет получает только автобус, опаздывающий больше чем на минуту
  const HOLD_MAX_S = 180;       // придержать можно не больше чем на 3 минуты
  const EXPRESS_STOPS = 4;      // экспрессом — пропустить следующие 4 остановки…
  const EXPRESS_SAVE_S = 30;    // …каждая пропущенная экономит ~30 с (торможение, посадка, разгон)
  const EXPRESS_MIN_S = 180;    // экспрессом пускают, только если опоздание больше 3 минут…
  const SHORT_TURN_MIN_S = 360; // …а разворачивают раньше конечной — только при опоздании больше 6 минут
  const FEATURES_FOR_REASON = {
    traffic_jam_ahead: ["traffic_score", "speed_avg_5min", "cur_dev_s", "hour_of_day"],
    long_dwell: ["dwell_last_stop_sec", "cur_dev_s", "hour_of_day", "headway_to_prev_sec"],
    speed_drop: ["speed_avg_5min", "speed_avg_15min", "distance_to_target_m", "cur_dev_s"],
    bunching: ["headway_to_next_sec", "headway_to_prev_sec", "cur_dev_s", "speed_avg_5min"],
    accumulated_delay: ["cur_dev_s", "current_delay_sec", "planned_time_to_target_sec", "traffic_score"],
  };

  const rnd = (a, b) => a + Math.random() * (b - a);
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  // Та же формула, что в контракте ML: risk = sigmoid((delay_pred - 120) / 60)
  const riskFromDelay = (d) => 1 / (1 + Math.exp(-(d - 120) / 60));

  // Одно направление маршрута: своя линия, свои остановки и светофоры (с позицией вдоль линии)
  function buildDir(raw, dirRaw, dirIdx) {
    const line = dirRaw.line;
    const cum = G.cumulative(line);
    // остановки — по порядку движения (как в реальном маршруте), проецируем без «прыжков назад»
    let prev = 0;
    const stops = dirRaw.stops.map((s, i) => {
      const p = G.projectAfter(line, cum, [s[1], s[2]], prev);
      prev = p.pos_m;
      return { stop_id: `${raw.route_id}-${dirIdx}-${i + 1}`, name: s[0], lat: p.point[0], lon: p.point[1], pos_m: p.pos_m };
    });
    // Светофоры: реальные координаты (OpenStreetMap), фазы — симуляция.
    // Цикл 80–100 с, зелёный 45–60% цикла, у каждого перекрёстка свой сдвиг фазы.
    const signals = ((App.MOCK_SIGNALS || {})[raw.route_id] || [])
      .map(([lat, lon], i) => {
        const p = G.project(line, cum, [lat, lon]);
        const seed = Math.abs(Math.sin((lat * 1e4 + lon * 1e4) * 12.9898)) * 1000;
        const cycle = 80 + (seed % 20);
        return {
          id: `${raw.route_id}-s${i + 1}`, lat, lon, pos_m: p.pos_m, off_m: p.off_m,
          cycle, green: cycle * (0.6 + (seed % 12) / 100), offset: seed % cycle, // на магистралях зелёный длиннее
        };
      })
      .filter((sg) => sg.off_m < 30) // светофор стоит на этой стороне дороги
      .sort((a, b) => a.pos_m - b.pos_m);
    const length = cum[cum.length - 1];
    // средняя задержка на светофорах на метр пути (для «прогноза модели»): P(красный) × средний остаток красного
    const avgWait = signals.reduce((s, sg) => { const red = sg.cycle - sg.green; return s + (red / sg.cycle) * (red / 2); }, 0);
    const waitPerM = length ? avgWait / length : 0;
    const name = `${stops[0].name} → ${stops[stops.length - 1].name}`;
    return { line, cum, length, stops, signals, name, waitPerM };
  }

  function buildRoute(raw) {
    const dirs = raw.dirs.map((d, i) => buildDir(raw, d, i));
    if (raw.loop) dirs[0].name = `по кругу от «${dirs[0].stops[0].name}»`;
    return { route_id: raw.route_id, name: raw.name, transport_type: raw.transport_type, loop: !!raw.loop, dirs };
  }

  App.createMockSource = function () {
    // Часы симуляции: могут идти быстрее реальных (кнопки ×1 / ×5 / ×20 на карте)
    let simNow = Date.now();
    let speedFactor = 5;
    App.now = () => simNow;
    const routes = App.MOCK_ROUTES.map(buildRoute);
    const routeById = Object.fromEntries(routes.map((r) => [r.route_id, r]));
    const vehicles = [];
    const byId = new Map();
    const alerts = new Map();
    let alertSeq = 91000;
    let timer = null;

    // ---------- ТС и рейсы ----------
    // Направление, по которому сейчас едет ТС (0 — «туда», 1 — «обратно»)
    const dirOf = (v) => routeById[v.route_id].dirs[v._d];
    // Расстояние от начала рейса: у каждого направления своя линия, позиция считается вдоль неё
    const along = (v, pos) => pos;
    // Плановое время (мс), когда ТС должно быть в точке d (метры от начала рейса)
    const planAt = (v, d) => v._anchor + (d / PLAN_SPEED) * 1000;

    let tripSeq = 0;
    function newTrip(v, delaySec) {
      // прошлый рейс запоминаем — по нему сверяем прогнозы, если рейс уже закончился
      if (v._facts) v._lastTrip = { id: v._tripId, facts: v._facts, anchor: v._anchor, d: v._d };
      v._tripId = ++tripSeq;
      v._noise = rnd(-40, 40);
      v._facts = {};
      v._anchor = simNow - delaySec * 1000 - (along(v, v._pos) / PLAN_SPEED) * 1000;
    }

    function startTrouble(v, strength = 1) {
      v._trouble = Math.floor(rnd(240, 720)); // проблема длится 4–12 минут
      v._reason = pick(REASONS.filter((r) => r !== "accumulated_delay"));
      v._troubleSpeed = rnd(7, 12) / strength;
      v._noise = rnd(-70, 70); // ошибка «модели» для этого случая (как у настоящей: MAE ~40–60 с)
      v._feats = makeFeatures(v._reason);
      v._conf = +rnd(0.62, 0.9).toFixed(2);
    }

    function makeFeatures(reason) {
      const w = [rnd(0.3, 0.45), rnd(0.15, 0.25), rnd(0.08, 0.15), rnd(0.03, 0.08)];
      return FEATURES_FOR_REASON[reason].map((name, i) => ({ name, contribution: +w[i].toFixed(2) }));
    }

    routes.forEach((r) => {
      // сколько ТС на линии — по длине маршрута (примерно одно на 3 км трассы)
      const total = r.dirs.reduce((s, d) => s + d.length, 0);
      const n = Math.max(4, Math.min(9, Math.round(total / 3000)));
      for (let i = 0; i < n; i++) {
        const v = {
          vehicle_id: String(Math.floor(rnd(120000, 139999))),
          route_id: r.route_id,
          _d: i % r.dirs.length,
          _pos: r.dirs[i % r.dirs.length].length * ((Math.floor(i / r.dirs.length) + rnd(0.1, 0.8)) / Math.ceil(n / r.dirs.length)),
          _trouble: 0,
          _reason: "accumulated_delay",
          _feats: makeFeatures("accumulated_delay"),
          _conf: +rnd(0.6, 0.85).toFixed(2),
          speed: rnd(17, 22),
          _speedAvg: 18,
        };
        newTrip(v, rnd(-40, 70));
        vehicles.push(v);
        byId.set(v.vehicle_id, v);
      }
    });
    // Несколько ТС сразу «в проблеме», чтобы на демо было что показать с первой секунды
    [1, 9, 14, 21].map((i) => vehicles[i % vehicles.length]).forEach((v, i) => {
      newTrip(v, rnd(110, 170));
      startTrouble(v, i === 3 ? 0.7 : 1);
      v._trouble = rnd(300, 700);
    });

    // dt — секунды симуляции
    function step(dt) {
      simNow += dt * 1000;
      const now = simNow;
      for (const v of vehicles) {
        const r = routeById[v.route_id];
        const D = r.dirs[v._d];
        if (v._trouble > 0) {
          v._trouble -= dt;
          v.speed = clamp(v._troubleSpeed + rnd(-1.5, 1.5), 2, 14);
          // «Резкое падение скорости перед перекрёстком»: в демо — дольше стоит на красном

        } else {
          if (v._reason !== "accumulated_delay") {
            // проблема закончилась, но опоздание ещё не отыграно
            v._reason = "accumulated_delay";
            v._feats = makeFeatures(v._reason);
          }
          // частота новых проблем — в реальном времени, чтобы при любой скорости симуляции было что показать
          if (Math.random() < (0.001 * (24 / vehicles.length) * Math.sqrt(speedFactor / 5) * dt) / speedFactor) startTrouble(v);
          // Водитель догоняет график, если опаздывает, и придерживается, если идёт с опережением
          const target = v.delay_now_sec > 20 ? CATCHUP_KMH : v.delay_now_sec < -20 ? 14 : 18;
          if (v.speed < 8) v.speed = 12; // тронулся после светофора
          v.speed = clamp(v.speed + (target - v.speed) * 0.3 + rnd(-1, 1), 10, 28);
        }
        v._speedAvg += (v.speed - v._speedAvg) * 0.08 * dt; // сглаженная скорость
        // Применённая мера: опоздание постепенно «отыгрывается» (сдвигаем плановое время)
        if (v._recover > 0) {
          const d = Math.min(v._recover, 1.2 * dt);
          v._anchor += d * 1000;
          v._recover -= d;
        }

        // Придержан на остановке: стоит, пока не выйдет время
        if (v._hold > 0) {
          v._hold -= dt;
          v.speed = 0;
        }
        // Экспресс: пропускает остановки — едет быстрее, пока не проедет последнюю пропускаемую
        if (v._express && v._hold <= 0) {
          v.speed = Math.max(v.speed, 24);
          if (v._pos >= v._express.untilPos) v._express = null;
        }

        // Движение по линии маршрута (реальное время)
        const before = along(v, v._pos);
        let move = v._hold > 0 ? 0 : (v.speed / 3.6) * dt;
        // Светофор впереди горит красным — останавливаемся перед стоп-линией (за 8 м)
        v._wait = null;
        for (const sg of D.signals) {
          const ds = along(v, sg.pos_m);
          if (ds <= before - 1 || ds > before + move + 8) continue;
          const st = signalState(r, sg);
          if (st.state !== "red") continue;
          // Условный приоритет: опаздывающему автобусу светофор продлевает зелёный или включает его раньше
          if (tspActive(r) && v.delay_now_sec > TSP_LATE_S && (st.sinceRed <= TSP_EXTEND_S || st.left <= TSP_EARLY_S)) {
            sg._grantUntil = simNow + Math.max(6000, 3000 * speedFactor); // «П» на карте — видно ~3 с при любой скорости
            continue;
          }
          move = Math.max(0, Math.min(move, ds - 8 - before));
          v._wait = { sig: sg, left: st.left };
          break;
        }
        // «Стоит на красном» — только когда реально остановился (а не подъезжает)
        if (v._wait && move < 0.5) {
          v._waitSince = v._waitSince || now;
          v.speed = 0;
        } else {
          v._wait = null;
          v._waitSince = null;
        }
        v._pos = clamp(v._pos + move, 0, D.length);
        // Фиксируем фактическое время прохождения остановок (включая конечную)
        const after = along(v, v._pos);
        for (const s of D.stops) {
          const d = along(v, s.pos_m);
          if (d > before && d <= after + 0.5) v._facts[s.stop_id] = now;
        }
        if (v._pos >= D.length) {
          // конечная: разворот, новый рейс в обратную сторону — по своей стороне дороги
          v._d = (v._d + 1) % r.dirs.length; // у кольцевого одно направление — едет дальше по кругу
          v._pos = 0;
          newTrip(v, rnd(-30, 60));
        }
        const DN = r.dirs[v._d];
        const [lat, lon] = G.pointAt(DN.line, DN.cum, v._pos);
        v.lat = lat;
        v.lon = lon;

        // Текущее отклонение и прогноз
        v.delay_now_sec = (now - planAt(v, along(v, v._pos))) / 1000;
        v.delay_pred_sec = Math.round(clamp(predDelay(v, HORIZON_S), -300, 900));
        v.risk_score = +riskFromDelay(v.delay_pred_sec).toFixed(3);
        v.updated_at = new Date(now).toISOString();
      }
    }

    // Прогноз опоздания через t секунд: текущее + тренд (насколько медленнее плана едем)
    // Прогноз опоздания через t секунд. В демо «модель» знает, сколько продлится проблема,
    // и ошибается на v._noise — как настоящая модель с MAE около 40–60 секунд.
    function predDelay(v, t) {
      return futureDelay(v, t) + (v._noise || 0) * Math.min(1, t / HORIZON_S);
    }
    // «Истинное» будущее опоздание при текущей обстановке
    function futureDelay(v, t) {
      let d = v.delay_now_sec;
      const tl = Math.max(0, v._trouble || 0);
      const tr = Math.min(t, tl);
      if (tr > 0) d += (1 - (v._troubleSpeed / 3.6) / PLAN_SPEED) * tr; // во время проблемы копится
      const rest = t - tr;
      if (v._recover > 0) d -= Math.min(v._recover, 1.2 * t);        // применённая мера
      // ожидаемые ожидания на светофорах впереди (если на маршруте не включён приоритет)
      const r = routeById[v.route_id];
      // с приоритетом опаздывающий ждёт на светофорах заметно меньше (но не ноль: продление ограничено)
      const tspK = tspActive(r) && v.delay_now_sec > TSP_LATE_S ? 0.35 : 1;
      d += r.dirs[v._d].waitPerM * PLAN_SPEED * t * tspK;
      if (v._hold > 0) d += v._hold;                                     // придержан — ещё постоит
      if (v._express) d -= Math.min(v._express.save, v._express.save * t / 300); // экономия на пропущенных остановках
      const catchUp = 1 - (CATCHUP_KMH / 3.6) / PLAN_SPEED;                     // < 0: догоняет график
      d = d > 0 ? Math.max(0, d + catchUp * rest) : Math.min(0, d - catchUp * rest);
      return d;
    }

    // Интервал до предыдущего ТС того же маршрута/направления (bus bunching, Daganzo 2009).
    // План: длина_направления / (n_ТС · plan_speed). Факт: (Δpos_m)/plan_speed.
    const headwayFor = (v) => {
      if (v._reserve) return { headway: null, plan: null };
      const peers = vehicles.filter((x) => !x._reserve && x.route_id === v.route_id && x._d === v._d);
      if (peers.length < 2) return { headway: null, plan: null };
      peers.sort((a, b) => a._pos - b._pos);
      const i = peers.findIndex((x) => x.vehicle_id === v.vehicle_id);
      const r = routeById[v.route_id]; const D = r && r.dirs[v._d];
      const plan = D ? Math.round(D.length / (peers.length * PLAN_SPEED)) : null;
      if (i >= peers.length - 1) return { headway: null, plan }; // впереди никого (в этом направлении)
      return { headway: Math.round((peers[i + 1]._pos - v._pos) / PLAN_SPEED), plan }; // до автобуса впереди
    };
    // Автобус, который едет следом (тот, кого можно придержать)
    const followerOf = (v) => {
      const peers = vehicles.filter((x) => !x._reserve && x.route_id === v.route_id && x._d === v._d && x._pos < v._pos);
      return peers.sort((a, b) => b._pos - a._pos)[0] || null;
    };
    // Рекомендация: при большом накопленном опоздании на радиальном маршруте — развернуть раньше конечной
    const recFor = (v) => {
      const reason = v._reason || "accumulated_delay";
      if (reason === "accumulated_delay" && v.delay_pred_sec > SHORT_TURN_MIN_S && !routeById[v.route_id].loop) return "short_turn";
      // «паровозик» лечат, придерживая догоняющего; если догоняющего нет — выравнивают интервалы
      if (reason === "bunching" && !holdFor(v, followerOf(v))) return "adjust_interval";
      return REC_FOR_REASON[reason];
    };
    const pub = (v) => {
      const level = App.riskLevel(v.risk_score);
      const { headway, plan } = headwayFor(v);
      const out = {
        vehicle_id: v.vehicle_id, route_id: v.route_id, direction_id: v._d, lat: v.lat, lon: v.lon, is_reserve: !!v._reserve,
        speed: Math.round(v.speed), heading: null,
        delay_now_sec: Math.round(v.delay_now_sec), delay_pred_sec: v.delay_pred_sec, risk_score: v.risk_score,
        updated_at: v.updated_at,
        headway_prev_sec: headway, plan_headway_sec: plan,
      };
      if (v._hold > 0) out.holding_sec = Math.round(v._hold);          // придержан на остановке
      if (v._express) out.express_until = v._express.untilName;        // идёт экспрессом
      // Стоит на красном — диспетчер видит, почему ТС не едет
      if (v._wait) {
        out.waiting_signal = {
          signal_id: v._wait.sig.id, lat: v._wait.sig.lat, lon: v._wait.sig.lon,
          waited_sec: Math.round((simNow - (v._waitSince || simNow)) / 1000), left_sec: v._wait.left,
        };
      }
      if (level !== "green") {
        const reason = v._reason || "accumulated_delay";
        Object.assign(out, {
          reason_pattern: reason,
          recommendation: recFor(v),
          top_features: v._feats,
          confidence: v._conf,
        });
      }
      return out;
    };

    // ---------- Расписание рейса ----------
    function schedule(v) {
      const r = routeById[v.route_id];
      const now = simNow;
      const dCur = along(v, v._pos);
      const ordered = r.dirs[v._d].stops;
      let nextFound = false;
      const rows = ordered.map((s) => {
        const d = along(v, s.pos_m);
        const plan = planAt(v, d);
        const row = { stop_id: s.stop_id, name: s.name, lat: s.lat, lon: s.lon, time_plan: new Date(plan).toISOString() };
        if (d <= dCur) {
          // Пройдена. Если прошли её до старта симуляции — «восстанавливаем» факт
          const fact = v._facts[s.stop_id] || plan + v.delay_now_sec * 1000 * (0.4 + 0.6 * (d / Math.max(dCur, 1)));
          Object.assign(row, { status: "passed", time_fact: new Date(fact).toISOString(), delay_sec: Math.round((fact - plan) / 1000) });
        } else {
          const ahead = (d - dCur) / PLAN_SPEED;
          const delay = predDelay(v, ahead);
          Object.assign(row, {
            status: nextFound ? "upcoming" : "next",
            time_pred: new Date(plan + delay * 1000).toISOString(),
            delay_sec: Math.round(delay),
            _ahead: ahead,
          });
          nextFound = true;
        }
        return row;
      });
      // Целевая остановка — первая, чьё плановое время попадает в окно T+10…15 мин
      const upcoming = rows.filter((x) => x.status !== "passed");
      const target = upcoming.find((x) => x._ahead > 600 && x._ahead <= 900)
        || upcoming.slice().sort((a, b) => Math.abs(a._ahead - HORIZON_S) - Math.abs(b._ahead - HORIZON_S))[0];
      if (target) target.is_target = true;
      rows.forEach((x) => delete x._ahead);
      return {
        vehicle_id: v.vehicle_id, route_id: v.route_id,
        direction: `${ordered[0].name} → ${ordered[ordered.length - 1].name}`,
        stops: rows,
      };
    }

    function makeAlert(v) {
      const sch = schedule(v);
      const t = sch.stops.find((s) => s.is_target) || sch.stops[sch.stops.length - 1];
      const p = pub(v);
      return {
        type: "alert.new",
        alert_id: `a-${++alertSeq}`,
        vehicle_id: v.vehicle_id,
        route_id: v.route_id,
        target_stop_id: t.stop_id,
        target_stop_name: t.name,
        delay_pred_sec: t.delay_sec != null ? t.delay_sec : v.delay_pred_sec, // прогноз именно для целевой остановки
        risk_score: v.risk_score,
        confidence: p.confidence,
        eta_incident: t.time_pred || t.time_plan,
        reason_pattern: p.reason_pattern,
        recommendation: p.recommendation,
        top_features: p.top_features,
        model_version: "ens-0.3.1 (mock)",
        created_at: new Date(simNow).toISOString(),
        _trip: v._tripId, _plan: new Date(t.time_plan).getTime(),
      };
    }

    // Состояние светофора сейчас: "green" | "red" | "priority" (включён приоритет для ОТ на маршруте)
    // Условный приоритет: фазы светофоров НЕ меняются для всех — только отдельный опаздывающий автобус
    // получает продление/ранний зелёный (см. step). «priority» — светофор прямо сейчас пропускает автобус.
    const tspActive = (route) => route._tspUntil > simNow;
    function signalState(route, sig) {
      const t = (simNow / 1000 + sig.offset) % sig.cycle;
      if (sig._grantUntil > simNow) return { state: "priority", left: Math.round((sig._grantUntil - simNow) / 1000) };
      return t < sig.green
        ? { state: "green", left: Math.round(sig.green - t) }
        : { state: "red", left: Math.round(sig.cycle - t), sinceRed: t - sig.green };
    }

    // Прогноз после меры (детерминированно, чтобы цифры не прыгали между расчётами).
    // target — ТС, для которого диспетчер выбирает меру (развернуть / экспресс / придержать того, кто за ним).
    function afterMeasure(v, scenario, target) {
      const before = v.delay_pred_sec;
      const r = routeById[v.route_id];
      const hash = [...v.vehicle_id].reduce((a, c) => a + c.charCodeAt(0), 0) % 10; // небольшой разброс по ТС
      const fits = MEASURE_FITS[scenario] && MEASURE_FITS[scenario].includes(v._reason);
      switch (scenario) {
        case "signal_priority": {
          // только опаздывающим; выигрыш — часть ожидания на светофорах за 10–15 мин
          // NYC: TSP в среднем −14% времени в пути (от 1 до 25%) — у нас не больше 15% от 12,5 мин
          if (v.delay_now_sec <= TSP_LATE_S) return before;
          const wait = r.dirs[v._d].waitPerM * PLAN_SPEED * HORIZON_S;
          return Math.round(before - Math.min(wait * 0.65 + (fits ? 15 : 0), 0.15 * HORIZON_S));
        }
        case "detour":
          // объезд помогает, только если впереди затор; иначе объезд длиннее обычного пути
          if (!target || v.vehicle_id !== target.vehicle_id) return before;
          return v._reason === "traffic_jam_ahead" ? Math.round(before * 0.7) : before + 60;
        case "short_turn":
          // развернуть раньше конечной: обратный рейс начнётся по графику; пассажиры до конечной пересядут
          if (!target || v.vehicle_id !== target.vehicle_id || r.loop || before < SHORT_TURN_MIN_S) return before;
          return Math.min(before, 20 + hash * 3);
        case "express":
          if (!target || v.vehicle_id !== target.vehicle_id || before < EXPRESS_MIN_S) return before;
          return Math.round(before - EXPRESS_STOPS * EXPRESS_SAVE_S);
        case "hold_at_stop": {
          // придерживаем того, кто ДОГОНЯЕТ target: он опоздает сильнее, зато интервал выровняется,
          // и target перестаёт собирать чужих пассажиров (меньше стоянки на остановках)
          if (!target) return before;
          const f = followerOf(target);
          const hold = holdFor(target, f);
          if (!hold) return before;
          if (f && v.vehicle_id === f.vehicle_id) return Math.round(before + hold * 0.6);
          if (v.vehicle_id === target.vehicle_id) return Math.round(before - hold * (fits ? 0.8 : 0.4));
          return before;
        }
        default: {
          if (before <= 30) return before; // идущим по графику мера не нужна
          let k = MEASURE_EFFECT[scenario] || 0;
          if (fits) k += 0.1;
          k = Math.min(0.3, k * (0.9 + hash / 50));
          return Math.round(before * (1 - k));
        }
      }
    }

    // Сколько придержать догоняющий автобус: до планового интервала не хватает (план − факт), но не больше 3 мин
    function holdFor(target, f) {
      if (!f) return 0;
      const plan = headwayFor(f).plan;
      const gap = (target._pos - f._pos) / PLAN_SPEED; // сек между follower и target
      if (!plan || gap >= plan * 0.8) return 0;         // интервал нормальный — держать незачем
      return Math.round(Math.min(HOLD_MAX_S, (plan - gap) * 0.6));
    }

    // Пояснение к мере в окне сравнения: к кому применяется и чем платим
    function measureNote(scenario, target) {
      const r = target && routeById[target.route_id];
      switch (scenario) {
        case "signal_priority": return "Светофоры продлевают зелёный (до 10 с) или включают его раньше (до 15 с) только опаздывающим автобусам. Остальной поток почти не замечает.";
        case "hold_at_stop": {
          const f = target && followerOf(target);
          const hold = holdFor(target, f);
          return f && hold
            ? `Придержать ТС ${f.vehicle_id} (едет следом) на ${Math.round(hold / 60 * 10) / 10} мин, чтобы не шли «паровозиком». Оно само опоздает сильнее.`
            : "Интервал до следующего автобуса нормальный — придерживать некого.";
        }
        case "detour": return target && target._reason === "traffic_jam_ahead" ? "Объехать затор впереди." : "Затора впереди нет — объезд будет дольше обычного пути.";
        case "short_turn":
          if (r && r.loop) return "Кольцевой маршрут — развернуть нельзя.";
          if (target && target.delay_pred_sec < SHORT_TURN_MIN_S) return "Разворачивают только при опоздании больше 6 минут — здесь не нужно.";
          return "Развернуть до конечной и сразу встать в график обратно. Пассажиры до конечной пересядут на следующий автобус.";
        case "express":
          if (target && target.delay_pred_sec < EXPRESS_MIN_S) return "Экспрессом пускают только при опоздании больше 3 минут — здесь не нужно.";
          return `Проехать следующие ${EXPRESS_STOPS} остановки без посадки. Людей с них заберёт следующий автобус.`;
        case "add_reserve": return "Выпустить резервный автобус с конечной в разрыв интервала.";
        case "adjust_interval": return "Выровнять интервалы между всеми автобусами маршрута.";
      }
      return "";
    }

    // Резервное ТС выходит с конечной и идёт по графику
    let reserveSeq = 1;
    function addReserve(route_id) {
      const r = routeById[route_id];
      const v = {
        vehicle_id: `Р${reserveSeq++}-${route_id}`, route_id, _reserve: true,
        _pos: 0, _d: 0, _trouble: 0, _reason: "accumulated_delay",
        _feats: makeFeatures("accumulated_delay"), _conf: 0.8, speed: 22, _speedAvg: 20,
      };
      newTrip(v, -20);
      vehicles.push(v);
      byId.set(v.vehicle_id, v);
      step(0);
      return v;
    }

    let offline = false;
    let handlers = null;
    let last = Date.now();
    function tick(h) {
      const real = Date.now();
      let left = Math.min(5, (real - last) / 1000) * speedFactor; // сколько секунд симуляции прошло
      last = real;
      while (left > 0) { const d = Math.min(2, left); step(d); left -= d; } // мелкими шагами, чтобы не «перепрыгивать» остановки
      // «Обрыв связи»: симуляция идёт дальше, но данные на дашборд не приходят
      if (offline) return;
      h.onVehicles && h.onVehicles(vehicles.map(pub));

      // Время инцидента наступило — сверяем прогноз с фактом
      for (const a of [...alerts.values()]) {
        if (new Date(a.eta_incident).getTime() > simNow) continue;
        const v = byId.get(a.vehicle_id);
        alerts.delete(a.alert_id);
        if (!v) continue;
        // Факт: если рейс тот же — из текущего расписания, если уже закончился — из прошлого рейса
        let factDelay = null;
        if (v._tripId === a._trip) {
          const st = schedule(v).stops.find((s) => s.stop_id === a.target_stop_id);
          if (st) factDelay = st.delay_sec;
        } else if (v._lastTrip && v._lastTrip.id === a._trip && v._lastTrip.facts[a.target_stop_id]) {
          factDelay = Math.round((v._lastTrip.facts[a.target_stop_id] - a._plan) / 1000);
        }
        if (factDelay == null) continue;
        const stop = { delay_sec: factDelay };
        h.onAlertVerified && h.onAlertVerified({
          type: "alert.verified", alert_id: a.alert_id, vehicle_id: a.vehicle_id, route_id: a.route_id,
          target_stop_id: a.target_stop_id, target_stop_name: a.target_stop_name,
          delay_pred_sec: a.delay_pred_sec, delay_fact_sec: stop.delay_sec,
          verified_at: new Date(simNow).toISOString(),
        });
      }

      for (const v of vehicles) {
        const has = [...alerts.values()].find((a) => a.vehicle_id === v.vehicle_id);
        if (!has && v.risk_score >= App.config.RISK_RED) {
          const a = makeAlert(v);
          // по одной остановке рейса — один алерт
          v._alerted = v._alerted && v._alerted.trip === v._tripId ? v._alerted : { trip: v._tripId, stops: new Set() };
          if (v._alerted.stops.has(a.target_stop_id)) { alertSeq--; continue; }
          // риск по прогнозу именно для целевой остановки; если он не «красный» — алерт не нужен
          a.risk_score = +riskFromDelay(a.delay_pred_sec).toFixed(3);
          if (a.risk_score < App.config.RISK_RED) { alertSeq--; continue; }
          v._alerted.stops.add(a.target_stop_id);
          alerts.set(a.alert_id, a);
          h.onAlertNew && h.onAlertNew(a);
        }
        // Алерт не снимаем раньше времени: в момент инцидента сверим прогноз с фактом (см. выше)
      }
    }

    return {
      name: "mock",
      async getRoutes() {
        return routes.map((r) => ({
          route_id: r.route_id, name: r.name, transport_type: r.transport_type,
          geometry: r.dirs[0].line,                               // линия «туда» (для совместимости)
          stops: r.dirs[0].stops.map(({ pos_m, ...s }) => s),
          // оба направления: каждое по своей стороне дороги
          directions: r.dirs.map((d, i) => ({
            direction_id: i, name: d.name, geometry: d.line,
            stops: d.stops.map(({ pos_m, ...s }) => s),
          })),
        }));
      },
      async getVehicles() {
        step(0);
        return vehicles.map(pub);
      },
      async getAlerts() {
        return [...alerts.values()];
      },
      async getSchedule(vehicleId) {
        const v = byId.get(vehicleId);
        if (!v) throw new Error("ТС не найдено");
        return schedule(v);
      },
      async getMetrics() {
        // Формат как у ML-сервиса (GET /metrics/model); MAE — реальный с labels_test (statistics/tables/model_metrics.csv)
        // uncertainty.coverage_calibrated_holdout — оценка покрытия 80%-интервала на тестовом фолде.
        return {
          mae_test_s: 43.7, latency_ms_p50: 18,
          model_version: "catboost-ensemble-v1 (mock)",
          uncertainty: { coverage_target: 0.80, coverage_calibrated_holdout: 0.82 },
          validation: {
            // MAE по горизонту прогноза: где модель точнее «на подлёте», где — за 10 мин
            mae_by_lead: [
              { bin: "0-3м", mae_s: 28 }, { bin: "3-5м", mae_s: 34 },
              { bin: "5-10м", mae_s: 44 }, { bin: "10-15м", mae_s: 58 },
              { bin: "15м+", mae_s: 71 },
            ],
          },
        };
      },
      async getWorstStops(limit = 10) {
        // Топ-10 по прогнозируемой задержке среди всех предстоящих остановок.
        const agg = new Map();
        for (const v of vehicles) {
          if (v._reserve) continue;
          const sch = schedule(v);
          for (const s of sch.stops) {
            if (s.status === "passed" || !Number.isFinite(s.delay_sec)) continue;
            const key = `${v.route_id}|${sch.direction_id ?? 0}|${s.stop_id}`;
            const cell = agg.get(key) || {
              route_id: v.route_id, direction_id: sch.direction_id ?? 0,
              stop_id: s.stop_id, name: s.name, lat: s.lat, lon: s.lon,
              vehicles: 0, sum: 0, max: 0,
            };
            cell.vehicles += 1; cell.sum += s.delay_sec;
            if (s.delay_sec > cell.max) cell.max = s.delay_sec;
            agg.set(key, cell);
          }
        }
        return [...agg.values()]
          .map((c) => ({
            route_id: c.route_id, direction_id: c.direction_id,
            stop_id: c.stop_id, name: c.name, lat: c.lat, lon: c.lon,
            avg_delay_sec: Math.round(c.sum / c.vehicles),
            max_delay_sec: Math.round(c.max),
            vehicles: c.vehicles,
          }))
          .filter((r) => r.avg_delay_sec >= 30)
          .sort((a, b) => b.avg_delay_sec - a.avg_delay_sec)
          .slice(0, limit);
      },
      // Bus bunching (Daganzo, 2009): пары ТС одного маршрута/направления
      // с фактическим интервалом < 60% · планового — «слипаются паровозиком».
      async getBunching() {
        const groups = new Map();
        for (const v of vehicles) {
          if (v._reserve) continue;
          const key = `${v.route_id}|${v._d ?? 0}`;
          if (!groups.has(key)) groups.set(key, []);
          groups.get(key).push(v);
        }
        const out = [];
        for (const [key, vs] of groups) {
          if (vs.length < 2) continue;
          const [route_id, dirStr] = key.split("|");
          const direction_id = +dirStr;
          const r = routeById[route_id]; if (!r) continue;
          const D = r.dirs[direction_id]; if (!D) continue;
          const planHeadway = D.length / (vs.length * PLAN_SPEED);
          const threshold = Math.max(45, planHeadway * 0.6);
          const sorted = [...vs].sort((a, b) => a._pos - b._pos);
          for (let i = 0; i < sorted.length - 1; i++) {
            const a = sorted[i], b = sorted[i + 1];
            const headway = (b._pos - a._pos) / PLAN_SPEED;
            if (headway < threshold) {
              out.push({
                route_id, direction_id,
                leader_id: b.vehicle_id, follower_id: a.vehicle_id, // b впереди (дальше по трассе), a догоняет
                headway_sec: Math.round(headway),
                plan_headway_sec: Math.round(planHeadway),
                ratio: +(headway / Math.max(planHeadway, 1)).toFixed(2),
                leader: { lat: b.lat, lon: b.lon },
                follower: { lat: a.lat, lon: a.lon },
              });
            }
          }
        }
        return out.sort((a, b) => a.ratio - b.ratio);
      },
      // What-if: как изменится прогноз у ТС маршрута, если применить меру
      async whatif({ scenario, route_id, at_stop_id, vehicle_id }) {
        await new Promise((res) => setTimeout(res, 300 + Math.random() * 400)); // «модель считает»
        const target = byId.get(vehicle_id) || null;
        const list = vehicles.filter((v) => v.route_id === route_id && !v._reserve).map((v) => {
          const before = v.delay_pred_sec, after = afterMeasure(v, scenario, target);
          return {
            vehicle_id: v.vehicle_id,
            delay_before_sec: before, delay_after_sec: after,
            risk_before: v.risk_score, risk_after: +riskFromDelay(after).toFixed(3),
          };
        });
        const avg = (key) => Math.round(list.reduce((s, x) => s + x[key], 0) / (list.length || 1));
        const red = (key) => list.filter((x) => x[key] >= App.config.RISK_RED).length;
        return {
          type: "whatif.result", scenario, route_id, at_stop_id,
          summary: {
            avg_delay_before_sec: avg("delay_before_sec"), avg_delay_after_sec: avg("delay_after_sec"),
            red_before: red("risk_before"), red_after: red("risk_after"),
          },
          note: measureNote(scenario, target),
          vehicles: list,
        };
      },
      // Применить меру в симуляции (только у мока; в живом режиме это решает диспетчер по-настоящему)
      async applyMeasure({ scenario, route_id, vehicle_id }) {
        const r = routeById[route_id];
        const target = byId.get(vehicle_id) || null;
        switch (scenario) {
          case "signal_priority":
            r._tspUntil = simNow + 20 * 60000; // 20 минут светофоры маршрута пропускают опаздывающих
            break;
          case "hold_at_stop": {
            const f = target && followerOf(target);
            const hold = holdFor(target, f);
            if (f && hold) { f._hold = hold; target._recover = (target._recover || 0) + hold * 0.4; }
            break;
          }
          case "short_turn":
            if (target && !r.loop && r.dirs.length > 1 && target.delay_pred_sec >= SHORT_TURN_MIN_S) {
              // разворот: встаёт в обратное направление примерно там же и идёт по графику
              const D = r.dirs[target._d], back = (target._d + 1) % r.dirs.length;
              target._pos = Math.max(0, r.dirs[back].length - target._pos * (r.dirs[back].length / D.length));
              target._d = back;
              target._trouble = 0;
              newTrip(target, 0);
              step(0);
            }
            break;
          case "express":
            if (target && target.delay_pred_sec >= EXPRESS_MIN_S) {
              const D = r.dirs[target._d];
              const ahead = D.stops.filter((st) => st.pos_m > target._pos).slice(0, EXPRESS_STOPS);
              if (ahead.length) {
                const last = ahead[ahead.length - 1];
                target._express = { untilPos: last.pos_m + 5, untilName: last.name, save: ahead.length * EXPRESS_SAVE_S };
                target._recover = (target._recover || 0) + ahead.length * EXPRESS_SAVE_S;
              }
            }
            break;
          case "detour":
            if (target && target._reason === "traffic_jam_ahead") {
              target._trouble = 0;
              target._recover = (target._recover || 0) + Math.max(0, target.delay_pred_sec - afterMeasure(target, "detour", target)) * 0.6;
            }
            break;
          default:
            for (const v of vehicles.filter((x) => x.route_id === route_id)) {
              const gain = v.delay_pred_sec - afterMeasure(v, scenario, target);
              if (gain > 0) v._recover = (v._recover || 0) + Math.max(0, v.delay_now_sec) * (gain / Math.max(v.delay_pred_sec, 1));
              if (MEASURE_FITS[scenario].includes(v._reason)) v._trouble = 0; // причина устранена
            }
            if (scenario === "add_reserve") addReserve(route_id);
        }
        return { ok: true };
      },
      // Светофоры маршрута с текущей фазой (только у мока: фазы — симуляция)
      getSignals(route_id) {
        const r = routeById[route_id];
        if (!r) return [];
        const seen = new Set(), out = [];
        for (const d of r.dirs) for (const sg of d.signals) {
          if (seen.has(sg.id)) continue;
          seen.add(sg.id);
          out.push({ signal_id: sg.id, lat: sg.lat, lon: sg.lon, ...signalState(r, sg) });
        }
        return out;
      },
      // Скорость симуляции (только у мока)
      setSpeed(x) { speedFactor = x; },
      getSpeed() { return speedFactor; },
      // Имитация обрыва связи с сервером (для демо критерия «надёжность»)
      setOffline(on) {
        offline = on;
        if (handlers) handlers.onStatus && handlers.onStatus(on ? "degraded" : "mock");
      },
      isOffline() { return offline; },
      start(h) {
        handlers = h;
        h.onStatus && h.onStatus("mock");
        last = Date.now();
        timer = setInterval(() => tick(h), 1000);
      },
      stop() {
        clearInterval(timer);
      },
    };
  };
})(window.App);
