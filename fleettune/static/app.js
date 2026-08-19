/* ==========================================================================
   FleetTune dashboard
   ========================================================================== */

const state = {
  vehicles: {},           // id -> latest summary
  vehicleOrder: [],       // id[]
  faultTypes: [],         // [{kind,label}]
  selectedId: null,
  alerts: [],
  alertFilter: 'all',
  maps: null,             // {fuel,timing,boost,tune} for selected vehicle
  mapsPrev: {},           // vid -> {fuel,timing,boost} last-seen matrices, for change highlighting
  mapsPollTimer: null,    // live-refresh interval while ECU maps tab is active
  markers: {},            // id -> Leaflet marker
  charts: {},             // chart instances
  chartData: {},          // per-vehicle rolling series
  pathHistory: {},        // vid -> [[lat,lng], ...] driven path trail
  pathLine: null,         // Leaflet polyline for the selected vehicle's trail
  showPath: true,
  canBuffer: [],          // scrolling CAN frames for selected vehicle
  canPaused: false,
  ws: null,
  wsFrames: null,
};

const MAX_PATH_POINTS = 500;

/* ---------- Boot ---------- */

document.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  setupAlertFilters();
  setupIgnitionBtn();
  setupTuneSliders();
  setupCanPause();
  initMap();
  connectWebSockets();
  setupStopButton();
});

/* ---------- Tabs ---------- */

function setupTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const name = btn.dataset.tab;
      document.querySelector(`.panel[data-panel="${name}"]`).classList.add('active');
      if (name === 'map' && state.leafletMap) {
        setTimeout(() => state.leafletMap.invalidateSize(), 60);
      }
      if (name === 'maps') {
        if (state.selectedId) { loadMaps(state.selectedId); startMapsPolling(); }
      } else {
        stopMapsPolling();
      }
      if (name === 'can' && state.selectedId) {
        document.getElementById('can-vid').textContent = state.selectedId;
        subscribeFrames(state.selectedId);
      }
    });
  });
}

/* ---------- WebSockets ---------- */

function connectWebSockets() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
  state.ws = ws;

  ws.onopen = () => setUplink(true);
  ws.onclose = () => { setUplink(false); setTimeout(connectWebSockets, 1500); };
  ws.onerror = () => setUplink(false);

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'hello') {
      state.faultTypes = msg.fault_types;
      msg.vehicles.forEach(v => {
        state.vehicles[v.id] = { id: v.id, label: v.label, driver: v.driver, telemetry: null };
        state.vehicleOrder.push(v.id);
      });
      renderVehicleList();
      if (!state.selectedId && state.vehicleOrder.length) selectVehicle(state.vehicleOrder[0]);
    } else if (msg.type === 'telemetry') {
      applyTelemetry(msg);
    }
  };
}

function subscribeFrames(vid) {
  if (state.wsFrames) { try { state.wsFrames.close(); } catch (e) {} state.wsFrames = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/frames?vehicle_id=${vid}`);
  state.wsFrames = ws;
  state.canBuffer = [];
  document.getElementById('can-body').innerHTML = '';
  ws.onmessage = (evt) => {
    if (state.canPaused) return;
    const msg = JSON.parse(evt.data);
    pushCanFrames(msg.frames, msg.ts);
  };
}

function setUplink(ok) {
  const dot = document.getElementById('pulse');
  const text = document.getElementById('stat-uplink');
  if (ok) { dot.classList.remove('off'); text.textContent = 'live'; }
  else { dot.classList.add('off'); text.textContent = 'reconnecting'; }
}

/* ---------- Telemetry apply ---------- */

function applyTelemetry(msg) {
  state.alerts = msg.alerts || [];
  const alertsByVeh = {};
  state.alerts.forEach(a => { (alertsByVeh[a.vehicle_id] ||= []).push(a); });

  msg.vehicles.forEach(v => {
    const prev = state.vehicles[v.id] || {};
    state.vehicles[v.id] = {
      ...prev,
      id: v.id, label: v.label, driver: v.driver, telemetry: v.telemetry,
      lat: v.lat, lng: v.lng, heading_deg: v.heading_deg,
      faults: v.faults, alerts: alertsByVeh[v.id] || [],
    };
    updateMapMarker(v.id);
    pushChartData(v.id, v.telemetry);
  });

  renderVehicleList();
  renderTopbarStats();
  renderAlerts();
  if (state.selectedId) renderDetail(state.selectedId);
}

/* ---------- Vehicle status classification ---------- */

function vehicleStatus(v) {
  const alerts = v.alerts || [];
  if (alerts.some(a => a.severity === 'critical')) return 'critical';
  if (alerts.some(a => a.severity === 'warn')) return 'warn';
  if (v.telemetry && !v.telemetry.ignition_on) return 'off';
  return 'ok';
}

/* ---------- Vehicle list ---------- */

function renderVehicleList() {
  const el = document.getElementById('vehicle-list');
  el.innerHTML = '';
  state.vehicleOrder.forEach(vid => {
    const v = state.vehicles[vid]; if (!v) return;
    const st = vehicleStatus(v);
    const item = document.createElement('div');
    item.className = 'vehicle-item' + (vid === state.selectedId ? ' active' : '');
    const dotClass = st === 'ok' ? '' : ' ' + st;
    const speed = v.telemetry ? v.telemetry.vehicle_speed_kph.toFixed(0) : '—';
    item.innerHTML = `
      <span class="status-dot${dotClass}"></span>
      <div class="vinfo">
        <div class="vid">${v.id}</div>
        <div class="vmeta">${v.driver.name} · ${v.driver.profile}</div>
      </div>
      <div class="vspeed">${speed}<span style="color:var(--text-mute);font-size:9px"> kph</span></div>
    `;
    item.addEventListener('click', () => selectVehicle(vid));
    el.appendChild(item);
  });
  document.getElementById('side-count').textContent = state.vehicleOrder.length;
}

function selectVehicle(vid) {
  state.selectedId = vid;
  renderVehicleList();
  if (state.leafletMap && state.markers[vid]) {
    state.leafletMap.setView(state.markers[vid].getLatLng(), Math.max(state.leafletMap.getZoom(), 11));
  }
  rebuildPathTrail(vid);
  renderDetail(vid);
  document.getElementById('can-vid').textContent = vid;
  document.getElementById('maps-vid').textContent = vid;
  document.getElementById('faults-vid').textContent = vid;
  renderFaults(vid);
  loadMaps(vid);
  if (document.querySelector('.tab.active').dataset.tab === 'can') subscribeFrames(vid);
}

/* ---------- Topbar stats ---------- */

function renderTopbarStats() {
  document.getElementById('stat-fleet').textContent = state.vehicleOrder.length;
  const moving = state.vehicleOrder.filter(id => {
    const t = state.vehicles[id]?.telemetry;
    return t && t.vehicle_speed_kph > 2;
  }).length;
  document.getElementById('stat-moving').textContent = moving;

  const alertsEl = document.getElementById('stat-alerts');
  const nAlerts = state.alerts.length;
  const crit = state.alerts.filter(a => a.severity === 'critical').length;
  alertsEl.textContent = nAlerts + (crit ? ` (${crit})` : '');
  alertsEl.classList.remove('high', 'critical');
  if (crit > 0) alertsEl.classList.add('critical');
  else if (nAlerts > 0) alertsEl.classList.add('high');

  const badge = document.getElementById('tab-alert-badge');
  if (nAlerts > 0) { badge.hidden = false; badge.textContent = nAlerts; }
  else badge.hidden = true;
}

/* ---------- Map ---------- */

function initMap() {
  const map = L.map('map', { zoomControl: true, preferCanvas: true })
    .setView([22.5, 78.9], 5);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '© OpenStreetMap · © CARTO',
    subdomains: 'abcd', maxZoom: 19,
  }).addTo(map);
  state.leafletMap = map;

  const legend = L.control({ position: 'bottomleft' });
  legend.onAdd = () => {
    const div = L.DomUtil.create('div', 'map-legend');
    div.innerHTML = `
      <div class="row"><span class="dot" style="background:var(--green)"></span>Nominal</div>
      <div class="row"><span class="dot" style="background:var(--amber)"></span>Warning</div>
      <div class="row"><span class="dot" style="background:var(--red)"></span>Critical</div>
      <div class="row"><span class="dot" style="background:var(--text-mute)"></span>Ignition off</div>
    `;
    return div;
  };
  legend.addTo(map);

  const pathCtrl = L.control({ position: 'topright' });
  pathCtrl.onAdd = () => {
    const div = L.DomUtil.create('div', 'map-path-ctrl');
    div.innerHTML = `
      <label class="path-toggle"><input type="checkbox" id="path-toggle-input" checked> Show path</label>
      <button type="button" id="path-clear-btn" class="path-clear-btn">Clear trail</button>
    `;
    L.DomEvent.disableClickPropagation(div);
    return div;
  };
  pathCtrl.addTo(map);
  document.getElementById('path-toggle-input').addEventListener('change', e => {
    state.showPath = e.target.checked;
    rebuildPathTrail(state.selectedId);
  });
  document.getElementById('path-clear-btn').addEventListener('click', () => {
    if (state.selectedId) state.pathHistory[state.selectedId] = [];
    rebuildPathTrail(state.selectedId);
  });
}

/* ---------- Vehicle path trail ---------- */

function recordPathPoint(vid, lat, lng) {
  const hist = (state.pathHistory[vid] ||= []);
  const last = hist[hist.length - 1];
  if (!last || last[0] !== lat || last[1] !== lng) {
    hist.push([lat, lng]);
    if (hist.length > MAX_PATH_POINTS) hist.shift();
  }
}

function appendPathPoint(vid, lat, lng) {
  if (!state.showPath || vid !== state.selectedId || !state.leafletMap) return;
  if (!state.pathLine) {
    state.pathLine = L.polyline([[lat, lng]], {
      color: '#4ecdc4', weight: 2, opacity: 0.8, dashArray: '1,6', lineCap: 'round',
    }).addTo(state.leafletMap);
  } else {
    state.pathLine.addLatLng([lat, lng]);
  }
}

function rebuildPathTrail(vid) {
  if (!state.leafletMap) return;
  if (state.pathLine) { state.leafletMap.removeLayer(state.pathLine); state.pathLine = null; }
  if (!state.showPath || !vid) return;
  const hist = state.pathHistory[vid] || [];
  if (hist.length < 2) return;
  state.pathLine = L.polyline(hist, {
    color: '#4ecdc4', weight: 2, opacity: 0.8, dashArray: '1,6', lineCap: 'round',
  }).addTo(state.leafletMap);
}

function updateMapMarker(vid) {
  const v = state.vehicles[vid];
  if (!v.lat || !v.lng) return;
  const st = vehicleStatus(v);
  const icon = L.divIcon({
    className: '', iconSize: [28, 28], iconAnchor: [14, 14],
    html: `<div class="truck-marker ${st === 'ok' ? '' : st}">${vid.split('-')[1]}</div>`,
  });
  if (state.markers[vid]) {
    state.markers[vid].setLatLng([v.lat, v.lng]);
    state.markers[vid].setIcon(icon);
  } else {
    const m = L.marker([v.lat, v.lng], { icon }).addTo(state.leafletMap);
    m.on('click', () => selectVehicle(vid));
    m.bindTooltip(() => {
      const vv = state.vehicles[vid]; const t = vv?.telemetry;
      if (!t) return vid;
      return `<b>${vid}</b> · ${vv.driver.name}<br>${t.vehicle_speed_kph.toFixed(0)} kph · ${t.engine_rpm.toFixed(0)} RPM · fuel ${t.fuel_level_pct.toFixed(0)}%`;
    });
    state.markers[vid] = m;
  }
  recordPathPoint(vid, v.lat, v.lng);
  appendPathPoint(vid, v.lat, v.lng);
}

/* ---------- Detail: gauges + charts + driver + DTCs ---------- */

const GAUGES = [
  { key: 'engine_rpm', label: 'RPM', unit: '', decimals: 0 },
  { key: 'vehicle_speed_kph', label: 'Speed', unit: 'kph', decimals: 0 },
  { key: 'current_gear', label: 'Gear', unit: '', decimals: 0,
    format: (v, tel) => (v === 0 ? 'N' : String(v)) + (tel.gear_shifting ? ' ⇄' : '') },
  { key: 'engine_load_pct', label: 'Load', unit: '%', decimals: 0 },
  { key: 'coolant_temp_c', label: 'Coolant', unit: '°C', decimals: 1,
    warn: v => v >= 95, crit: v => v >= 105 },
  { key: 'oil_temp_c', label: 'Oil temp', unit: '°C', decimals: 1 },
  { key: 'engine_oil_pressure_kpa', label: 'Oil pressure', unit: 'kPa', decimals: 0,
    warn: v => v < 200, crit: v => v < 120 },
  { key: 'boost_pressure_kpa', label: 'Boost', unit: 'kPa', decimals: 0 },
  { key: 'exhaust_gas_temp_c', label: 'EGT', unit: '°C', decimals: 0 },
  { key: 'fuel_rate_lph', label: 'Fuel rate', unit: 'L/h', decimals: 1 },
  { key: 'fuel_level_pct', label: 'Fuel level', unit: '%', decimals: 1,
    warn: v => v < 20, crit: v => v < 8 },
  { key: 'battery_voltage', label: 'Battery', unit: 'V', decimals: 1,
    warn: v => v < 24, crit: v => v < 22 },
  { key: 'total_vehicle_distance_km', label: 'Odometer', unit: 'km', decimals: 0 },
];

function renderDetail(vid) {
  const v = state.vehicles[vid]; if (!v || !v.telemetry) return;
  document.getElementById('detail-title').textContent = v.id + ' · ' + v.label.split('·')[1].trim();
  document.getElementById('detail-sub').textContent =
    `lat ${v.lat.toFixed(4)}, lng ${v.lng.toFixed(4)} · heading ${v.heading_deg.toFixed(0)}° · ${v.telemetry.ignition_on ? 'ignition ON' : 'ignition OFF'}`;

  // Gauges
  const gaugesEl = document.getElementById('gauges');
  gaugesEl.innerHTML = '';
  GAUGES.forEach(g => {
    const val = v.telemetry[g.key];
    let cls = '';
    if (g.crit && g.crit(val)) cls = 'critical';
    else if (g.warn && g.warn(val)) cls = 'warn';
    const shown = g.format ? g.format(val, v.telemetry) : Number(val).toFixed(g.decimals);
    const div = document.createElement('div');
    div.className = 'gauge';
    div.innerHTML = `<div class="gauge-label">${g.label}</div>
      <div class="gauge-value ${cls}">${shown}<span class="gauge-unit">${g.unit}</span></div>`;
    gaugesEl.appendChild(div);
  });

  // Ignition button
  const ib = document.getElementById('btn-ignition');
  ib.textContent = 'Ignition: ' + (v.telemetry.ignition_on ? 'ON' : 'OFF');
  ib.classList.toggle('on', v.telemetry.ignition_on);

  // Driver
  document.getElementById('d-name').textContent = v.driver.name;
  document.getElementById('d-profile').textContent = v.driver.profile;
  document.getElementById('d-cont').textContent = v.driver.continuous_drive_h.toFixed(2) + ' h';
  const fatEl = document.getElementById('d-fatigue');
  fatEl.textContent = v.driver.fatigue_score.toFixed(0) + ' / 100';
  fatEl.classList.remove('warn', 'critical');
  if (v.driver.fatigue_score >= 75) fatEl.classList.add('critical');
  else if (v.driver.fatigue_score >= 55) fatEl.classList.add('warn');
  document.getElementById('d-lanes').textContent = v.driver.lane_departures_recent;
  const fill = document.getElementById('fatigue-fill');
  fill.style.width = Math.min(100, v.driver.fatigue_score) + '%';
  fill.style.background = v.driver.fatigue_score >= 75 ? 'var(--red)'
                        : v.driver.fatigue_score >= 55 ? 'var(--amber)' : 'var(--green)';

  // Driving behavior (harsh events, idle waste, composite score)
  const score = v.driver.driver_score ?? 100;
  const scoreEl = document.getElementById('d-score');
  scoreEl.textContent = score.toFixed(0) + ' / 100';
  scoreEl.classList.remove('warn', 'critical');
  if (score < 50) scoreEl.classList.add('critical');
  else if (score < 75) scoreEl.classList.add('warn');
  const scoreFill = document.getElementById('score-fill');
  scoreFill.style.width = Math.min(100, score) + '%';
  scoreFill.style.background = score < 50 ? 'var(--red)' : score < 75 ? 'var(--amber)' : 'var(--green)';
  document.getElementById('d-harsh-brake').textContent = v.driver.harsh_brake_count ?? 0;
  document.getElementById('d-harsh-accel').textContent = v.driver.harsh_accel_count ?? 0;
  document.getElementById('d-idle').textContent = (v.driver.idle_minutes ?? 0).toFixed(1) + ' min';
  document.getElementById('d-idle-fuel').textContent = (v.driver.idle_fuel_l ?? 0).toFixed(2) + ' L';

  // ECU power factor (fuel map -> throttle response), shown on the ECU maps tab
  updateTunePowerFactor(v.telemetry);

  // DTCs
  const dtcList = document.getElementById('dtc-list');
  const dtcs = v.telemetry.active_dtcs || [];
  if (dtcs.length === 0) {
    dtcList.innerHTML = '<div class="empty">No active codes</div>';
  } else {
    dtcList.innerHTML = dtcs.map(d =>
      `<div class="dtc"><span class="spn">SPN ${d.spn} / FMI ${d.fmi}</span> — ${d.desc}</div>`).join('');
  }

  updateCharts(vid);
}

/* ---------- Chart rings ---------- */

function pushChartData(vid, tel) {
  if (!tel) return;
  if (!state.chartData[vid]) {
    state.chartData[vid] = { labels: [], rpm: [], speed: [], coolant: [], oil: [], egt: [],
                              fuel: [], fuelRate: [], boost: [], load: [] };
  }
  const d = state.chartData[vid];
  const t = new Date().toLocaleTimeString([], { hour12: false });
  const MAX = 60;
  d.labels.push(t);
  d.rpm.push(tel.engine_rpm);
  d.speed.push(tel.vehicle_speed_kph);
  d.coolant.push(tel.coolant_temp_c);
  d.oil.push(tel.oil_temp_c);
  d.egt.push(tel.exhaust_gas_temp_c);
  d.fuel.push(tel.fuel_level_pct);
  d.fuelRate.push(tel.fuel_rate_lph);
  d.boost.push(tel.boost_pressure_kpa);
  d.load.push(tel.engine_load_pct);
  ['labels','rpm','speed','coolant','oil','egt','fuel','fuelRate','boost','load'].forEach(k => {
    if (d[k].length > MAX) d[k].shift();
  });
}

function ensureCharts() {
  if (state.charts.rpm) return;
  const commonOpts = () => ({
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { labels: { color: '#8ba3bc', font: { family: 'JetBrains Mono', size: 10 } } } },
    scales: {
      x: { ticks: { color: '#536b83', font: { size: 9, family: 'JetBrains Mono' }, maxTicksLimit: 6 },
           grid: { color: 'rgba(43,74,103,0.3)' } },
      y: { ticks: { color: '#8ba3bc', font: { size: 10, family: 'JetBrains Mono' } },
           grid: { color: 'rgba(43,74,103,0.3)' } },
    },
    elements: { point: { radius: 0 }, line: { tension: 0.35, borderWidth: 1.5 } },
  });

  state.charts.rpm = new Chart(document.getElementById('chart-rpm'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'RPM', data: [], borderColor: '#f5a623', yAxisID: 'y' },
      { label: 'Speed kph', data: [], borderColor: '#4ecdc4', yAxisID: 'y1' },
    ]},
    options: { ...commonOpts(), scales: {
      ...commonOpts().scales,
      y: { ...commonOpts().scales.y, position: 'left', title: { display: true, text: 'RPM', color: '#8ba3bc' } },
      y1: { ...commonOpts().scales.y, position: 'right', grid: { display: false }, title: { display: true, text: 'kph', color: '#8ba3bc' } },
    }}
  });

  state.charts.thermal = new Chart(document.getElementById('chart-thermal'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Coolant', data: [], borderColor: '#4ade80' },
      { label: 'Oil', data: [], borderColor: '#a78bfa' },
      { label: 'EGT', data: [], borderColor: '#f5a623' },
    ]},
    options: commonOpts(),
  });

  state.charts.fuel = new Chart(document.getElementById('chart-fuel'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Fuel level %', data: [], borderColor: '#4ecdc4', yAxisID: 'y' },
      { label: 'Fuel rate L/h', data: [], borderColor: '#f5a623', yAxisID: 'y1' },
    ]},
    options: { ...commonOpts(), scales: {
      ...commonOpts().scales,
      y: { ...commonOpts().scales.y, position: 'left', min: 0, max: 100 },
      y1: { ...commonOpts().scales.y, position: 'right', grid: { display: false }, beginAtZero: true },
    }}
  });

  state.charts.boost = new Chart(document.getElementById('chart-boost'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Boost kPa', data: [], borderColor: '#f5a623' },
      { label: 'Load %', data: [], borderColor: '#4ade80' },
    ]},
    options: commonOpts(),
  });
}

function updateCharts(vid) {
  ensureCharts();
  const d = state.chartData[vid]; if (!d) return;
  const set = (chart, mapping) => {
    chart.data.labels = d.labels;
    chart.data.datasets.forEach((ds, i) => { ds.data = d[mapping[i]]; });
    chart.update('none');
  };
  set(state.charts.rpm, ['rpm', 'speed']);
  set(state.charts.thermal, ['coolant', 'oil', 'egt']);
  set(state.charts.fuel, ['fuel', 'fuelRate']);
  set(state.charts.boost, ['boost', 'load']);
}

/* ---------- Ignition ---------- */

function setupIgnitionBtn() {
  document.getElementById('btn-ignition').addEventListener('click', () => {
    const v = state.vehicles[state.selectedId]; if (!v) return;
    const on = !(v.telemetry?.ignition_on);
    fetch('/api/ignition', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({vehicle_id: v.id, on})
    });
  });
}

/* ---------- CAN stream ---------- */

function setupCanPause() {
  document.getElementById('can-pause').addEventListener('change', e => state.canPaused = e.target.checked);
}

function pushCanFrames(frames, ts) {
  const tbody = document.getElementById('can-body');
  const time = new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) +
               '.' + String(Math.floor((ts % 1) * 1000)).padStart(3, '0');
  for (const f of frames) {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${time}</td>
      <td class="id">${f.can_id}</td>
      <td>${f.priority}</td>
      <td class="pgn">${f.pgn}</td>
      <td>${f.name}</td>
      <td>${String(f.sa).padStart(2,'0')}</td>
      <td class="data">${f.data}</td>`;
    tbody.appendChild(row);
  }
  while (tbody.children.length > 500) tbody.removeChild(tbody.firstChild);
  const scroll = document.querySelector('.can-scroll');
  scroll.scrollTop = scroll.scrollHeight;
}

/* ---------- ECU maps + tune ---------- */

function cloneMatrix(m) { return m.map(row => row.slice()); }

function diffMatrix(prev, curr) {
  const changed = new Map(); // "row,col" -> delta
  if (!prev) return changed;
  for (let r = 0; r < curr.length; r++) {
    for (let c = 0; c < curr[r].length; c++) {
      const delta = curr[r][c] - (prev[r]?.[c] ?? curr[r][c]);
      if (Math.abs(delta) > 0.05) changed.set(`${r},${c}`, delta);
    }
  }
  return changed;
}

function startMapsPolling() {
  stopMapsPolling();
  state.mapsPollTimer = setInterval(() => {
    if (state.selectedId) loadMaps(state.selectedId);
  }, 2500);
}

function stopMapsPolling() {
  if (state.mapsPollTimer) { clearInterval(state.mapsPollTimer); state.mapsPollTimer = null; }
}

async function loadMaps(vid) {
  const r = await fetch(`/api/vehicle/${vid}/maps`);
  if (!r.ok) return;
  const m = await r.json();
  const prev = state.mapsPrev[vid];
  state.maps = m;
  renderMap('map-fuel', 'fuel', m.fuel, [10, 100], diffMatrix(prev?.fuel, m.fuel));
  renderMap('map-timing', 'timing', m.timing, [0, 25], diffMatrix(prev?.timing, m.timing));
  renderMap('map-boost', 'boost', m.boost, [0, 250], diffMatrix(prev?.boost, m.boost));
  state.mapsPrev[vid] = { fuel: cloneMatrix(m.fuel), timing: cloneMatrix(m.timing), boost: cloneMatrix(m.boost) };
  // Sliders
  const bind = (id, val) => {
    const el = document.getElementById(id);
    el.value = val;
    document.getElementById(id + '-val').textContent = Number(val).toFixed(id === 't-trim' ? 1 : 0);
  };
  bind('t-idle', m.tune.idle_rpm_target);
  bind('t-rev', m.tune.rev_limit_rpm);
  bind('t-gov', m.tune.speed_governor_kph);
  bind('t-trim', m.tune.fuel_trim_pct);
  loadTuneEvents(vid);
}

/* ---------- ECU tuning -> driver behavior log ---------- */

function updateTunePowerFactor(tel) {
  const el = document.getElementById('tune-power-factor');
  if (!el || !tel || tel.ecu_power_factor == null) return;
  const pf = tel.ecu_power_factor;
  const note = pf < 0.92 ? ' — detuned, less torque per throttle input'
             : pf > 1.08 ? ' — over-fueled, reaches the limiter sooner'
             : ' — near stock';
  el.textContent = (pf * 100).toFixed(0) + '%' + note;
}

async function loadTuneEvents(vid) {
  const r = await fetch(`/api/vehicle/${vid}/tune-events`);
  if (!r.ok) return;
  const { events } = await r.json();
  if (vid === state.selectedId) renderTuneEvents(vid, events);
}

function renderTuneEvents(vid, events) {
  const el = document.getElementById('tune-events-list');
  if (!el) return;
  if (!events.length) { el.innerHTML = '<div class="empty">No ECU edits yet this session</div>'; return; }
  const v = state.vehicles[vid];
  const liveFatigue = v?.driver?.fatigue_score;
  const livePedal = v?.telemetry?.accel_pedal_pct;
  const fmt = (n) => (n >= 0 ? '+' : '') + n.toFixed(0);
  el.innerHTML = events.slice().reverse().map(ev => {
    const time = new Date(ev.ts * 1000).toLocaleTimeString([], { hour12: false });
    const fatDelta = liveFatigue != null ? liveFatigue - ev.fatigue_at_change : null;
    const pedalDelta = livePedal != null ? livePedal - ev.pedal_at_change : null;
    const fatCls = fatDelta > 2 ? 'up' : fatDelta < -2 ? 'down' : '';
    const pedCls = pedalDelta > 5 ? 'up' : pedalDelta < -5 ? 'down' : '';
    return `
      <div class="tune-event">
        <span class="te-time">${time}</span>
        <span class="te-delta ${fatCls}">fatigue ${ev.fatigue_at_change.toFixed(0)} → ${liveFatigue != null ? liveFatigue.toFixed(0) : '—'} <b>${fatDelta != null ? fmt(fatDelta) : ''}</b></span>
        <span class="te-delta ${pedCls}">pedal ${ev.pedal_at_change.toFixed(0)}% → ${livePedal != null ? livePedal.toFixed(0) : '—'}% <b>${pedalDelta != null ? fmt(pedalDelta) + '%' : ''}</b></span>
        <div class="te-detail">${ev.detail}</div>
      </div>`;
  }).join('');
}

function heatColor(v, lo, hi) {
  const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
  // deep navy -> amber -> red
  const stops = [
    { t: 0,   r: 13,  g: 40,  b: 65  },
    { t: 0.5, r: 200, g: 130, b: 30  },
    { t: 1,   r: 210, g: 60,  b: 60  },
  ];
  let a = stops[0], b = stops[stops.length - 1];
  for (let i = 0; i < stops.length - 1; i++) {
    if (t >= stops[i].t && t <= stops[i+1].t) { a = stops[i]; b = stops[i+1]; break; }
  }
  const lt = (t - a.t) / (b.t - a.t);
  const r = Math.round(a.r + (b.r - a.r) * lt);
  const g = Math.round(a.g + (b.g - a.g) * lt);
  const bl = Math.round(a.b + (b.b - a.b) * lt);
  return `rgb(${r},${g},${bl})`;
}

function renderMap(elId, which, matrix, range, changed = new Map()) {
  const el = document.getElementById(elId);
  el.innerHTML = '';
  // Reverse rows so load increases upward
  const rowsReversed = matrix.map((_, i) => matrix[matrix.length - 1 - i]);
  rowsReversed.forEach((row, ri) => {
    row.forEach((val, ci) => {
      const r = matrix.length - 1 - ri;
      const cell = document.createElement('div');
      cell.className = 'cell';
      cell.style.background = heatColor(val, range[0], range[1]);
      cell.textContent = Number(val).toFixed(0);
      cell.title = `RPM bin ${ci} · Load bin ${r} · ${val}`;
      cell.dataset.row = String(r);
      cell.dataset.col = String(ci);
      const delta = changed.get(`${r},${ci}`);
      if (delta !== undefined) {
        cell.classList.add('cell-changed', delta > 0 ? 'delta-up' : 'delta-down');
        cell.dataset.delta = (delta > 0 ? '+' : '') + delta.toFixed(1);
      }
      cell.addEventListener('click', () => editCell(cell, which, matrix, range));
      el.appendChild(cell);
    });
  });
}

function editCell(cell, which, matrix, range) {
  if (cell.classList.contains('editing')) return;
  const r = +cell.dataset.row, c = +cell.dataset.col;
  const current = matrix[r][c];
  cell.classList.add('editing');
  cell.innerHTML = `<input type="number" step="0.5" value="${current}">`;
  const input = cell.querySelector('input');
  input.focus(); input.select();
  const commit = async () => {
    const val = parseFloat(input.value);
    const paletteRange = which === 'fuel' ? [10, 100] : which === 'timing' ? [0, 25] : [0, 250];
    if (!isNaN(val) && val !== current) {
      const delta = val - current;
      matrix[r][c] = val;
      await fetch('/api/map', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({vehicle_id: state.selectedId, which, matrix})
      });
      renderMap('map-' + which, which, matrix, paletteRange, new Map([[`${r},${c}`, delta]]));
      if (state.mapsPrev[state.selectedId]) state.mapsPrev[state.selectedId][which] = cloneMatrix(matrix);
      loadTuneEvents(state.selectedId);
      return;
    }
    // Repaint entire map with new range awareness (use the *max of range and edited value*)
    renderMap('map-' + which, which, matrix, paletteRange);
  };
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') { cell.classList.remove('editing'); const p = which === 'fuel' ? [10,100] : which==='timing'?[0,25]:[0,250]; renderMap('map-'+which, which, matrix, p);} });
}

function setupTuneSliders() {
  const send = (params) => fetch('/api/tune', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({vehicle_id: state.selectedId, ...params})
  });
  const wire = (id, key, fmt) => {
    const el = document.getElementById(id);
    const out = document.getElementById(id + '-val');
    el.addEventListener('input', () => { out.textContent = fmt(el.value); });
    el.addEventListener('change', () => { send({[key]: parseFloat(el.value)}).then(() => loadTuneEvents(state.selectedId)); });
  };
  wire('t-idle', 'idle_rpm_target', v => Number(v).toFixed(0));
  wire('t-rev',  'rev_limit_rpm',   v => Number(v).toFixed(0));
  wire('t-gov',  'speed_governor_kph', v => Number(v).toFixed(0));
  wire('t-trim', 'fuel_trim_pct',   v => (v > 0 ? '+' : '') + Number(v).toFixed(1) + ' %');
}

/* ---------- Alerts ---------- */

function setupAlertFilters() {
  document.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.alertFilter = chip.dataset.filter;
      renderAlerts();
    });
  });
}

/* ---------- Stop / shutdown ---------- */

function setupStopButton() {
  const btn = document.getElementById('btn-stop');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!confirm('Stop simulation and quit the server?')) return;
    try {
      btn.disabled = true;
      btn.textContent = 'Stopping...';
      // Attempt to notify server to stop — server will terminate shortly after responding
      await fetch('/api/stop', { method: 'POST' });
    } catch (e) {
      // best-effort; disable button so user doesn't repeatedly hit it
      btn.textContent = 'Stopping...';
    }
    // Close websockets as a fallback to stop UI activity
    try { if (state.ws) state.ws.close(); if (state.wsFrames) state.wsFrames.close(); } catch (e) {}
  });
}

function renderAlerts() {
  const el = document.getElementById('alerts-list');
  const filtered = state.alertFilter === 'all' ?
    state.alerts : state.alerts.filter(a => a.category === state.alertFilter);
  if (!filtered.length) {
    el.innerHTML = '<div class="empty">No active alerts</div>';
    return;
  }
  el.innerHTML = filtered.map(a => `
    <div class="alert ${a.severity}">
      <div class="alert-badge">${a.severity} · ${a.category}</div>
      <div>
        <div class="alert-title">${a.title} <span style="color:var(--text-mute);font-family:var(--mono);font-size:11px">· ${a.vehicle_id}</span></div>
        <div class="alert-detail">${a.detail}</div>
      </div>
      <div class="alert-meta">${new Date(a.last_seen_iso).toLocaleTimeString([], { hour12: false })}</div>
    </div>
  `).join('');
}

/* ---------- Fault injection ---------- */

function renderFaults(vid) {
  const el = document.getElementById('faults-list');
  el.innerHTML = '';
  const v = state.vehicles[vid]; if (!v) return;
  state.faultTypes.forEach(ft => {
    const phase = (v.faults || {})[ft.kind] || 'inactive';
    const card = document.createElement('div');
    card.className = 'fault-card';
    card.innerHTML = `
      <div class="fault-info">
        <div class="fault-name">${ft.label}</div>
        <div class="fault-phase ${phase}">${phase}</div>
      </div>
      <button class="fault-toggle ${phase !== 'inactive' ? 'on' : ''}">${phase !== 'inactive' ? 'Clear' : 'Inject'}</button>
    `;
    card.querySelector('.fault-toggle').addEventListener('click', async () => {
      const url = phase !== 'inactive' ? '/api/fault/clear' : '/api/fault/inject';
      await fetch(url, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({vehicle_id: vid, kind: ft.kind}),
      });
      // Fault status updates arrive with next telemetry frame; re-render optimistically
      setTimeout(() => renderFaults(vid), 700);
    });
    el.appendChild(card);
  });
}
