// Карта на MapLibre GL: подложка с дорогами, маршруты, ТС, выделенный маршрут с остановками.
// Внимание: MapLibre хочет координаты как [lon, lat], а у нас везде [lat, lon] — переворачиваем через ll().
window.App = window.App || {};

(function (App) {
  const COLORS = { red: "#ff5a52", yellow: "#ffb020", green: "#2fd27a", passed: "#8b97ad", grey: "#56647e" };
  const ll = (p) => [p[1], p[0]];
  const byLevel = (fallback) => ["match", ["get", "level"], "red", COLORS.red, "yellow", COLORS.yellow, "green", COLORS.green, "passed", COLORS.passed, fallback];

  let map;
  let handlers = {};
  const markers = new Map(); // vehicle_id -> {marker, el, from, to, t0, level}
  let selectedId = null;
  let riskMode = true;  // цвет риска на линиях маршрутов включён по умолчанию
  let popup;
  let ready = false;       // подложка и наши слои загружены
  let pendingRoutes = null; // маршруты, пришедшие раньше, чем загрузилась карта
  let visible = null;       // Set route_id видимых линий или null = все

  const empty = { type: "FeatureCollection", features: [] };

  App.map = {
    COLORS,

    init(cfg, h) {
      handlers = h;
      map = new maplibregl.Map({
        container: "map",
        style: App.basemapStyle(),
        center: ll(cfg.MAP_CENTER),
        zoom: cfg.MAP_ZOOM,
        attributionControl: false,
        pitchWithRotate: false,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      map.addControl(new maplibregl.AttributionControl({
        compact: true,
        customAttribution: '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> · © <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>',
      }), "bottom-right");

      popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 18, className: "veh-pop", maxWidth: "260px" });

      map.on("error", (e) => console.error("Ошибка карты:", e && e.error ? e.error.message : e));

      map.on("click", (e) => {
        if (e.originalEvent.target.closest && e.originalEvent.target.closest(".veh")) return;
        handlers.onEmptyClick && handlers.onEmptyClick();
      });

      requestAnimationFrame(animate);

      // Размер точек зависит от масштаба: при отдалении зелёные становятся точками,
      // жёлтые — меньше, красные не меняются (проблему видно при любом масштабе)
      const zoomClass = () => {
        const z = map.getZoom();
        const c = map.getContainer().classList;
        c.toggle("zoom-far", z < 11.5);
        c.toggle("zoom-mid", z >= 11.5 && z < 13);
      };
      map.on("zoom", zoomClass);
      zoomClass();

      return new Promise((resolve) => map.on("load", () => {
        addOwnLayers();
        ready = true;
        if (pendingRoutes) this.drawRoutes(pendingRoutes);
        this.setVisibleRoutes(visible);
        this.setRiskMode(riskMode);
        resolve();
      }));
    },

    drawRoutes(routes) {
      this._routes = routes;
      if (!ready) { pendingRoutes = routes; return; }
      map.getSource("routes").setData({
        type: "FeatureCollection",
        features: routeFeatures(routes, {}),
      });
      this._routes = routes;
      const b = new maplibregl.LngLatBounds();
      routes.forEach((r) => r.geometry.forEach((p) => b.extend(ll(p))));
      if (!b.isEmpty()) map.fitBounds(b, { padding: 60, duration: 0 });
      const shown = visible ? routes.filter((r) => visible.has(r.route_id)) : routes;
      if (shown.length && shown.length < routes.length) this.fitRoutes(shown, 0);
    },

    // Уровни риска маршрутов: {route_id: "red" | "yellow" | "green"}
    setRouteLevels(levels) {
      if (!this._routes || !ready) return;
      map.getSource("routes").setData({
        type: "FeatureCollection",
        features: routeFeatures(this._routes, levels),
      });
    },

    // Показать только выбранные линии (null — все)
    setVisibleRoutes(set) {
      visible = set;
      for (const m of markers.values()) m.el.style.display = isShown(m.data.route_id) ? "" : "none";
      if (!ready) return;
      map.setFilter("routes-line", set ? ["in", ["get", "route_id"], ["literal", [...set]]] : null);
    },

    // Пересчитать размер карты после изменения раскладки страницы
    resize() { if (map) map.resize(); },

    fitRoutes(routes, duration = 700) {
      if (!routes.length) return;
      const b = new maplibregl.LngLatBounds();
      routes.forEach((r) => r.geometry.forEach((p) => b.extend(ll(p))));
      map.fitBounds(b, { padding: 70, maxZoom: 14, duration });
    },

    // Переключатель «риск на маршрутах»: цветные линии вместо серых
    setRiskMode(on) {
      riskMode = on;
      if (!ready) return;
      map.setPaintProperty("routes-line", "line-color", on ? byLevel(COLORS.grey) : COLORS.grey);
    },

    updateVehicles(list) {
      const now = performance.now();
      for (const v of list) {
        if (v.lat == null || v.lon == null) continue;
        const level = App.riskLevel(v.risk_score);
        let m = markers.get(v.vehicle_id);
        if (!m) {
          const el = document.createElement("button");
          el.className = "veh" + (v.is_reserve ? " veh--reserve" : "");
          el.setAttribute("aria-label", `ТС ${v.vehicle_id}, маршрут ${App.routeLabel(v.route_id, v.direction_id)}`);
          el.innerHTML = `<span>${v.is_reserve ? "Р" : App.esc(App.routeLabel(v.route_id, v.direction_id))}</span>`;
          if (v.is_reserve) el.title = "Резервное ТС";
          el.addEventListener("click", (ev) => { ev.stopPropagation(); handlers.onVehicle && handlers.onVehicle(v.vehicle_id); });
          el.addEventListener("mouseenter", () => showPopup(v.vehicle_id));
          el.addEventListener("mouseleave", () => popup.remove());
          const marker = new maplibregl.Marker({ element: el }).setLngLat([v.lon, v.lat]).addTo(map);
          m = { marker, el, from: [v.lon, v.lat], to: [v.lon, v.lat], t0: now, dur: 1000, lastAt: now, data: v, dirId: v.direction_id };
          markers.set(v.vehicle_id, m);
        } else if (v.lon !== m.to[0] || v.lat !== m.to[1]) {
          // Растягиваем анимацию на реальный интервал между апдейтами (traffic.csv шлёт пинги
          // редко и неравномерно — 3-15с при CLOCK_SPEED=5). Так маркер едет всё это время,
          // а не «стоит-прыгает». Первый апдейт после создания: fallback 1 с. Клемпим [0.4с..15с],
          // чтобы NDTP-шторм (100Гц) не превращал маркер в желе, а редкий пинг не тянулся минуту.
          const dt = Math.max(400, Math.min(15000, now - m.lastAt));
          const cur = m.marker.getLngLat();
          m.from = [cur.lng, cur.lat];
          m.to = [v.lon, v.lat];
          m.t0 = now;
          m.dur = dt;
          m.lastAt = now;
        }
        m.data = v;
        if (m.dirId !== v.direction_id) {
          // ТС переехало на другой рейс/маршрут (см. RouteCatalog.direction — считается заново на каждый опрос):
          // перерисовываем бейдж, иначе на нём так и останется номер маршрута, с которым точка была создана.
          m.dirId = v.direction_id;
          m.el.setAttribute("aria-label", `ТС ${v.vehicle_id}, маршрут ${App.routeLabel(v.route_id, v.direction_id)}`);
          if (!v.is_reserve) m.el.querySelector("span").textContent = App.routeLabel(v.route_id, v.direction_id);
        }
        m.el.style.display = isShown(v.route_id) ? "" : "none";
        if (m.level !== level) {
          m.el.classList.remove("veh--red", "veh--yellow", "veh--green");
          m.el.classList.add(`veh--${level}`);
          m.level = level;
        }
        m.el.style.zIndex = level === "red" ? 3 : level === "yellow" ? 2 : 1;
        if (v.vehicle_id === selectedId) m.el.style.zIndex = 10;
      }
      if (popup.isOpen() && popup._vid) showPopup(popup._vid);
    },

    // Выделить ТС: подсветить его маршрут цветом прогноза и показать остановки
    select(vehicle, route, schedule) {
      selectedId = vehicle.vehicle_id;
      for (const [id, m] of markers) {
        m.el.classList.toggle("is-selected", id === selectedId);
        m.el.classList.toggle("is-dim", id !== selectedId);
      }
      if (ready) map.setPaintProperty("routes-line", "line-opacity", 0.18);
      this.updateSelection(vehicle, route, schedule);
    },

    updateSelection(vehicle, route, schedule) {
      if (!ready || !route || !schedule || vehicle.vehicle_id !== selectedId) return;
      // линия того направления, по которому едет ТС («туда» / «обратно» — по разным сторонам дороги)
      const dir = route.directions && route.directions.find((d) => d.direction_id === (vehicle.direction_id ?? 0));
      const line = dir ? dir.geometry : route.geometry;
      const key = "_cum" + (dir ? dir.direction_id : "");
      const cum = route[key] || (route[key] = App.geo.cumulative(line));
      const vPos = App.geo.project(line, cum, [vehicle.lat, vehicle.lon]).pos_m;
      const stops = schedule.stops.map((s) => ({ ...s, pos: App.geo.project(line, cum, [s.lat, s.lon]).pos_m }));

      const seg = (a, b, level) => ({
        type: "Feature",
        properties: { level },
        geometry: { type: "LineString", coordinates: App.geo.slice(line, cum, a, b).map(ll) },
      });
      const feats = [];
      const firstPos = stops.length ? stops[0].pos : vPos;
      feats.push(seg(firstPos, vPos, "passed"));
      let prev = vPos;
      for (const s of stops.filter((x) => x.status !== "passed")) {
        feats.push(seg(prev, s.pos, App.delayLevel(s.delay_sec)));
        prev = s.pos;
      }
      map.getSource("sel-line").setData({ type: "FeatureCollection", features: feats });
      map.getSource("sel-stops").setData({
        type: "FeatureCollection",
        features: stops.map((s) => ({
          type: "Feature",
          properties: {
            name: s.name,
            label: s.is_target ? `${s.name} · ${App.fmtDelayShort(s.delay_sec)}` : s.name,
            level: s.status === "passed" ? "passed" : App.delayLevel(s.delay_sec),
            target: !!s.is_target,
          },
          geometry: { type: "Point", coordinates: [s.lon, s.lat] },
        })),
      });
    },

    // Светофоры выбранного маршрута (список с текущей фазой) — обновляется раз в секунду
    updateSignals(list) {
      if (!ready) return;
      map.getSource("signals").setData({
        type: "FeatureCollection",
        features: (list || []).map((s) => ({
          type: "Feature",
          properties: { state: s.state, left: s.left, sel: !!s.sel },
          geometry: { type: "Point", coordinates: [s.lon, s.lat] },
        })),
      });
    },

    clearSelection() {
      selectedId = null;
      for (const m of markers.values()) m.el.classList.remove("is-selected", "is-dim");
      if (!ready) return;
      map.getSource("sel-line").setData(empty);
      map.getSource("sel-stops").setData(empty);
      map.setPaintProperty("routes-line", "line-opacity", 0.85);
    },

    // Подсветить «паровозики»: пары ТС одного маршрута с малым интервалом
    setBunchingPairs(pairs) {
      const ids = new Set();
      for (const p of pairs || []) { ids.add(p.leader_id); ids.add(p.follower_id); }
      for (const [id, m] of markers) m.el.classList.toggle("veh--bunch", ids.has(id));
    },

    focusRoute(route, vehicle) {
      const b = new maplibregl.LngLatBounds();
      route.geometry.forEach((p) => b.extend(ll(p)));
      if (vehicle) b.extend([vehicle.lon, vehicle.lat]);
      map.fitBounds(b, { padding: { top: 70, bottom: 70, left: 70, right: 70 }, maxZoom: 14.5, duration: 700 });
    },
  };

  const isShown = (routeId) => !visible || visible.has(routeId);

  // Линии маршрутов: оба направления (если бэкенд их отдаёт), иначе одна линия
  function routeFeatures(routes, levels) {
    const out = [];
    for (const r of routes) {
      const lines = r.directions && r.directions.length ? r.directions.map((d) => d.geometry) : [r.geometry];
      for (const g of lines) out.push({
        type: "Feature",
        properties: { route_id: r.route_id, level: levels[r.route_id] || "green" },
        geometry: { type: "LineString", coordinates: g.map(ll) },
      });
    }
    return out;
  }

  // Рисуем значок светофора на canvas (в 2 раза крупнее — для чёткости на экранах с высокой плотностью)
  function signalIcon(state) {
    const W = 28, H = 50, c = document.createElement("canvas");
    c.width = W; c.height = H;
    const g = c.getContext("2d");
    const box = (x, y, w, h, r) => { g.beginPath(); g.roundRect(x, y, w, h, r); };
    // корпус
    box(3, 3, W - 6, H - 6, 8);
    g.fillStyle = "#0b1019"; g.fill();
    g.lineWidth = state === "priority" ? 4 : 2.5;
    g.strokeStyle = state === "priority" ? "#ffffff" : "#7f8ca3"; g.stroke();
    // лампы
    const lamp = (cy, color, on) => {
      g.beginPath(); g.arc(W / 2, cy, 7.5, 0, Math.PI * 2);
      if (on) { g.shadowColor = color; g.shadowBlur = 10; g.fillStyle = color; }
      else { g.shadowBlur = 0; g.fillStyle = "#2a3346"; }
      g.fill(); g.shadowBlur = 0;
    };
    lamp(15, "#ff4d45", state === "red");
    lamp(H - 15, "#2fe07f", state !== "red");
    return g.getImageData(0, 0, W, H);
  }

  function addOwnLayers() {
    map.addSource("routes", { type: "geojson", data: empty });
    map.addSource("sel-line", { type: "geojson", data: empty });
    map.addSource("sel-stops", { type: "geojson", data: empty });
    map.addSource("signals", { type: "geojson", data: empty });

    // Все маршруты — серые линии поверх дорог
    map.addLayer({
      id: "routes-line", type: "line", source: "routes",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": COLORS.grey,
        "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 14, 2.5, 17, 3.5],
        "line-opacity": 0.85,
      },
    });
    // Выделенный маршрут: свечение + линия по участкам
    map.addLayer({
      id: "sel-glow", type: "line", source: "sel-line",
      filter: ["!=", ["get", "level"], "passed"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": byLevel(COLORS.grey), "line-width": ["interpolate", ["linear"], ["zoom"], 10, 5, 14, 8, 17, 10], "line-blur": 6, "line-opacity": 0.35 },
    });
    map.addLayer({
      id: "sel-passed", type: "line", source: "sel-line",
      filter: ["==", ["get", "level"], "passed"],
      paint: { "line-color": COLORS.passed, "line-width": 2, "line-opacity": 0.6, "line-dasharray": [1.5, 1.5] },
    });
    map.addLayer({
      id: "sel-line", type: "line", source: "sel-line",
      filter: ["!=", ["get", "level"], "passed"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": byLevel(COLORS.grey), "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 14, 3.5, 17, 4.5] },
    });
    // Светофоры: реальные места (OpenStreetMap), фазы — симуляция.
    // Значок как у настоящего светофора: тёмный корпус, горит красная или зелёная лампа.
    ["red", "green", "priority"].forEach((st) => map.addImage(`sig-${st}`, signalIcon(st), { pixelRatio: 2 }));
    const sigIcon = ["concat", "sig-", ["get", "state"]];
    map.addLayer({
      id: "signals-all", type: "symbol", source: "signals", minzoom: 13,
      filter: ["!=", ["get", "sel"], true],
      layout: {
        "icon-image": sigIcon, "icon-allow-overlap": true, "icon-ignore-placement": true,
        "icon-size": ["interpolate", ["linear"], ["zoom"], 13, 0.6, 16, 0.9],
      },
      paint: { "icon-opacity": 0.85 },
    });
    map.addLayer({
      id: "signals", type: "symbol", source: "signals",
      filter: ["==", ["get", "sel"], true],
      layout: {
        "icon-image": sigIcon, "icon-allow-overlap": true, "icon-ignore-placement": true,
        "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.95, 12, 1.1, 15, 1.4],
        // сколько секунд до переключения — при приближении
        "text-field": ["case", ["==", ["get", "state"], "priority"], "П", ["to-string", ["get", "left"]]],
        "text-font": ["Noto Sans Bold"],
        "text-size": 12,
        "text-offset": [1.3, 0],
        "text-anchor": "left",
        "text-allow-overlap": true,
        "text-optional": true,
      },
      paint: {
        "text-color": ["match", ["get", "state"], "red", "#ffb4ae", "#9ff0c4"],
        "text-halo-color": "#0b1019", "text-halo-width": 1.5,
        "text-opacity": ["step", ["zoom"], 0, 13.5, 1],
      },
    });
    // Остановки выделенного маршрута
    map.addLayer({
      id: "sel-stops", type: "circle", source: "sel-stops",
      paint: {
        "circle-radius": ["case", ["get", "target"], 8, 5],
        "circle-color": "#0d131f",
        "circle-stroke-color": byLevel(COLORS.grey),
        "circle-stroke-width": ["case", ["get", "target"], 3.5, 2.5],
      },
    });
    map.addLayer({
      id: "sel-stop-labels", type: "symbol", source: "sel-stops",
      layout: {
        "text-field": ["get", "label"],
        "text-font": ["case", ["get", "target"], ["literal", ["Noto Sans Bold"]], ["literal", ["Noto Sans Regular"]]],
        "text-size": ["case", ["get", "target"], 15, 13],
        "text-anchor": "left",
        "text-offset": [1, 0],
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": ["case", ["get", "target"], byLevel("#e7edf6"), "#dfe6f1"],
        "text-halo-color": "#0d131f",
        "text-halo-width": 1.6,
      },
    });
  }

  function showPopup(id) {
    const m = markers.get(id);
    if (!m) return;
    const v = m.data;
    const level = App.riskLevel(v.risk_score);
    const reason = v.reason_pattern ? `<div class="pop__reason">${App.esc(App.labels.t("reasons", v.reason_pattern))}</div>` : "";
    const wait = v.waiting_signal ? `<div class="pop__wait">Стоит на красном · ${v.waiting_signal.waited_sec} с</div>` : "";
    popup.setLngLat(m.marker.getLngLat()).setHTML(
      `<div class="pop"><div class="pop__head"><span class="route-chip">${App.esc(App.routeLabel(v.route_id, v.direction_id))}</span> ТС ${App.esc(v.vehicle_id)}</div>
       <div class="pop__row">сейчас <b>${App.fmtDelayShort(v.delay_now_sec)}</b> · через 10–15 мин <b class="t-${level}">${App.fmtDelayShort(v.delay_pred_sec)}</b></div>
       ${wait}${reason}<div class="pop__hint">нажмите, чтобы открыть</div></div>`
    ).addTo(map);
    popup._vid = id;
  }

  // Плавное движение маркеров между обновлениями — линейно интерполируем от m.from к m.to
  // за m.dur мс (см. updateVehicles). Обновляем DOM только пока анимация идёт: когда k=1
  // застыл — раз выставили финальную позицию и до следующего апдейта не трогаем.
  function animate(t) {
    for (const m of markers.values()) {
      const k = Math.min(1, (t - m.t0) / m.dur);
      if (k < 1 || m._k !== 1) {
        m.marker.setLngLat([m.from[0] + (m.to[0] - m.from[0]) * k, m.from[1] + (m.to[1] - m.from[1]) * k]);
        m._k = k;
      }
    }
    requestAnimationFrame(animate);
  }
})(window.App);
