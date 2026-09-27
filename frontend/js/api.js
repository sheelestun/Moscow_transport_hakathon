// Живой источник данных: REST + WebSocket бэкенда (раздел 7.2 ARCHITECTURE_AND_ROLES.md).
// Интерфейс тот же, что у мока: getRoutes / getVehicles / getAlerts / getMetrics / whatif / start.
window.App = window.App || {};

(function (App) {
  App.createLiveSource = function (cfg) {
    const base = cfg.API_BASE.replace(/\/$/, "");

    async function get(path) {
      const r = await fetch(base + path);
      if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
      return r.json();
    }

    // Бэкенд может вернуть либо массив, либо {items: [...]} — поддержим оба
    const list = (x, key) => (Array.isArray(x) ? x : x[key] || x.items || []);

    return {
      name: "live",
      async getRoutes() { return list(await get("/routes"), "routes"); },
      async getVehicles() { return list(await get("/vehicles"), "vehicles"); },
      async getAlerts() { return list(await get("/alerts?active=true"), "alerts"); },
      async getMetrics() { return get("/metrics/model"); },
      async getWorstStops(limit = 10) {
        try { return await get(`/metrics/worst_stops?limit=${limit}`); }
        catch { return []; }
      },
      async getBunching() {
        try { return await get("/metrics/bunching"); }
        catch { return []; }
      },
      async getSchedule(id) { return get(`/vehicles/${encodeURIComponent(id)}/schedule`); },
      async getSignals(route_id) {
        try { return await get(`/routes/${encodeURIComponent(route_id)}/signals`); }
        catch { return []; }
      },
      async getRouteHours(route_id) {
        try { return await get(`/routes/${encodeURIComponent(route_id)}/hours`); }
        catch { return null; }
      },
      async whatif(body) {
        const r = await fetch(base + "/whatif", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(`/whatif: HTTP ${r.status}`);
        return r.json();
      },

      // Подписка на WebSocket с автопереподключением.
      // Если связь пропала — статус "degraded": UI показывает последнее известное состояние.
      start(h) {
        let delay = 1000;
        const connect = () => {
          const ws = new WebSocket(cfg.WS_URL);
          ws.onopen = () => { delay = 1000; h.onStatus && h.onStatus("live"); };
          ws.onmessage = (e) => {
            let msg;
            try { msg = JSON.parse(e.data); } catch { return; }
            switch (msg.type) {
              case "vehicle.update":
                // одно ТС или пачка {vehicles: [...]}
                h.onVehicles && h.onVehicles(msg.vehicles || [msg]);
                break;
              case "alert.new":
                h.onAlertNew && h.onAlertNew(msg);
                break;
              case "alert.verified":
                h.onAlertVerified && h.onAlertVerified(msg);
                break;
              case "alert.resolved":
                h.onAlertResolved && h.onAlertResolved(msg.alert_id);
                break;
              case "whatif.result":
                h.onWhatif && h.onWhatif(msg);
                break;
            }
          };
          ws.onclose = () => {
            h.onStatus && h.onStatus("degraded");
            setTimeout(connect, delay);
            delay = Math.min(delay * 2, 15000); // 1с, 2с, 4с … максимум 15с
          };
          ws.onerror = () => ws.close();
        };
        connect();
      },
    };
  };

  // Выбор источника по настройке MODE
  App.createSource = function (cfg) {
    return cfg.MODE === "live" ? App.createLiveSource(cfg) : App.createMockSource();
  };
})(window.App);
