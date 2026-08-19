async function fetchConfig(){
  const r = await fetch('/api/admin/config');
  if (!r.ok) return;
  const j = await r.json();
  document.getElementById('cfg').textContent = JSON.stringify(j, null, 2);
  document.getElementById('inp-n').value = j.configured_n ?? j.n_vehicles ?? '';
  document.getElementById('inp-ts').value = j.time_scale ?? '';
}

async function reconfigure(){
  const n = document.getElementById('inp-n').value;
  const ts = document.getElementById('inp-ts').value;
  const payload = {};
  if (n) payload.n_vehicles = Number(n);
  if (ts) payload.time_scale = Number(ts);
  const r = await fetch('/api/admin/reconfigure', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  if (!r.ok) { alert('failed to apply'); return; }
  await fetchConfig();
}

async function startSim(){
  await fetch('/api/admin/start', {method:'POST'});
  await fetchConfig();
}
async function stopSim(){
  await fetch('/api/admin/stop', {method:'POST'});
  await fetchConfig();
}

async function fetchLogStatus(){
  const r = await fetch('/api/admin/logging/status');
  if (!r.ok) return;
  const j = await r.json();
  document.getElementById('log-status').textContent = j.running ? 'logging' : 'stopped';
  document.getElementById('log-rows').textContent = j.rows ?? 0;
  document.getElementById('log-vehicles').textContent = j.vehicles ?? 0;
  document.getElementById('log-path').textContent = j.path ?? '—';
}

async function startLogging(){
  await fetch('/api/admin/logging/start', {method:'POST'});
  await fetchLogStatus();
}
async function stopLogging(){
  await fetch('/api/admin/logging/stop', {method:'POST'});
  await fetchLogStatus();
}

document.addEventListener('DOMContentLoaded', ()=>{
  document.getElementById('btn-reconf').addEventListener('click', reconfigure);
  document.getElementById('btn-start-sim').addEventListener('click', startSim);
  document.getElementById('btn-stop-sim').addEventListener('click', stopSim);
  document.getElementById('btn-log-start').addEventListener('click', startLogging);
  document.getElementById('btn-log-stop').addEventListener('click', stopLogging);
  fetchConfig();
  fetchLogStatus();
  setInterval(fetchConfig, 2500);
  setInterval(fetchLogStatus, 2500);
});