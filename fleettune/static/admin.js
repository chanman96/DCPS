// Tracks fields the user has started editing so the periodic config poll
// doesn't clobber an in-progress edit (previously it reset the input on every
// 2.5s tick, corrupting whatever the user was typing).
const dirty = { 'inp-n': false, 'inp-ts': false, 'em-host': false, 'em-port': false, 'em-user': false, 'em-to': false, 'em-sev': false };

async function fetchConfig(){
  const r = await fetch('/api/admin/config');
  if (!r.ok) return;
  const j = await r.json();
  document.getElementById('cfg').textContent = JSON.stringify(j, null, 2);
  const nEl = document.getElementById('inp-n');
  const tsEl = document.getElementById('inp-ts');
  if (!dirty['inp-n']) nEl.value = j.configured_n ?? j.n_vehicles ?? '';
  if (!dirty['inp-ts']) tsEl.value = j.time_scale ?? '';
}

async function reconfigure(){
  const n = document.getElementById('inp-n').value;
  const ts = document.getElementById('inp-ts').value;
  const payload = {};
  if (n) payload.n_vehicles = Number(n);
  if (ts) payload.time_scale = Number(ts);
  const r = await fetch('/api/admin/reconfigure', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  if (!r.ok) { alert('failed to apply'); return; }
  dirty['inp-n'] = false;
  dirty['inp-ts'] = false;
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

async function fetchEmailStatus(){
  const r = await fetch('/api/admin/email/status');
  if (!r.ok) return;
  const j = await r.json();
  document.getElementById('em-status').textContent = j.configured ? 'configured — sending live' : 'not configured';
  document.getElementById('em-sent').textContent = j.sent_count ?? 0;
  document.getElementById('em-error').textContent = j.last_error ?? '—';
  if (!dirty['em-host']) document.getElementById('em-host').value = j.smtp_host ?? '';
  if (!dirty['em-port']) document.getElementById('em-port').value = j.smtp_port ?? '';
  if (!dirty['em-user']) document.getElementById('em-user').value = j.smtp_user ?? '';
  if (!dirty['em-to'])   document.getElementById('em-to').value   = j.to_addr ?? '';
  if (!dirty['em-sev'])  document.getElementById('em-sev').value  = j.min_severity ?? 'warn';
}

async function saveEmailConfig(){
  const port = document.getElementById('em-port').value;
  const payload = {
    smtp_host:     document.getElementById('em-host').value || undefined,
    smtp_port:     port ? Number(port) : undefined,
    smtp_user:     document.getElementById('em-user').value || undefined,
    smtp_password: document.getElementById('em-password').value || undefined,
    to_addr:       document.getElementById('em-to').value || undefined,
    min_severity:  document.getElementById('em-sev').value || undefined,
  };
  const r = await fetch('/api/admin/email/configure', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  if (!r.ok) { alert('failed to save email settings'); return; }
  document.getElementById('em-password').value = '';
  ['em-host', 'em-port', 'em-user', 'em-to', 'em-sev'].forEach(id => { dirty[id] = false; });
  await fetchEmailStatus();
}

async function testEmail(){
  const btn = document.getElementById('btn-em-test');
  btn.disabled = true;
  const prevText = btn.textContent;
  btn.textContent = 'Sending…';
  try {
    const r = await fetch('/api/admin/email/test', {method:'POST'});
    const j = await r.json();
    alert(j.ok ? 'Test email sent.' : ('Failed: ' + (j.error || 'unknown error')));
  } finally {
    btn.disabled = false;
    btn.textContent = prevText;
    await fetchEmailStatus();
  }
}

document.addEventListener('DOMContentLoaded', ()=>{
  document.getElementById('btn-reconf').addEventListener('click', reconfigure);
  document.getElementById('btn-start-sim').addEventListener('click', startSim);
  document.getElementById('btn-stop-sim').addEventListener('click', stopSim);
  document.getElementById('btn-log-start').addEventListener('click', startLogging);
  document.getElementById('btn-log-stop').addEventListener('click', stopLogging);
  document.getElementById('btn-em-save').addEventListener('click', saveEmailConfig);
  document.getElementById('btn-em-test').addEventListener('click', testEmail);
  ['inp-n', 'inp-ts'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => { dirty[id] = true; });
  });
  ['em-host', 'em-port', 'em-user', 'em-to'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => { dirty[id] = true; });
  });
  document.getElementById('em-sev').addEventListener('change', () => { dirty['em-sev'] = true; });
  fetchConfig();
  fetchLogStatus();
  fetchEmailStatus();
  setInterval(fetchConfig, 2500);
  setInterval(fetchLogStatus, 2500);
  setInterval(fetchEmailStatus, 2500);
});
