"""The "Deploy to a Linux server" window: local HTML with its own bridge (`remote/api.py`).

Four steps: where the server is, who it says it is (host key), how to sign in and what it can
take, then the installation. Nothing typed here is kept after the window closes.
"""

DEPLOY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Deploy to a Linux server</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 20px 22px; background: #09090b; color: #e4e4e7; font: 14px system-ui, "Segoe UI", sans-serif; }
  h1 { font-size: 18px; margin: 0 0 4px; color: #fafafa; }
  h2 { font-size: 14px; margin: 18px 0 8px; color: #fafafa; }
  p { color: #a1a1aa; margin: 0 0 12px; font-size: 13px; line-height: 1.5; }
  label { font-size: 12px; color: #d4d4d8; display: block; margin-bottom: 10px; }
  input[type=text], input[type=password], input[type=number] { width: 100%; margin-top: 4px; padding: 7px 10px; border-radius: 8px; border: 1px solid #3f3f46; background: #09090b; color: #fafafa; font: inherit; }
  input:focus { outline: none; border-color: #10b981; }
  .row { display: grid; grid-template-columns: 1fr 110px; gap: 10px; }
  .pick { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: end; }
  button { font: inherit; padding: 7px 14px; border-radius: 8px; border: 1px solid #3f3f46; background: #27272a; color: #f4f4f5; cursor: pointer; }
  button:hover { background: #3f3f46; }
  button:disabled { opacity: .5; cursor: default; }
  button.primary { background: #059669; border-color: #10b981; color: #fff; }
  .buttons { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  .choice { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border: 1px solid #27272a; border-radius: 10px; margin-bottom: 8px; font-size: 13px; color: #e4e4e7; }
  .choice.off { opacity: .55; }
  .choice small { display: block; color: #a1a1aa; margin-top: 2px; }
  .box { border: 1px solid #27272a; border-radius: 10px; padding: 12px 14px; background: #18181b; margin-bottom: 12px; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 13px; color: #fafafa; word-break: break-all; }
  .warn { border-color: #92400e; background: #1c1407; color: #fcd34d; font-size: 13px; }
  .bad { border-color: #7f1d1d; background: #1c0a0a; color: #fca5a5; font-size: 13px; }
  .note { color: #a1a1aa; font-size: 12px; margin: 0 0 6px; }
  pre { margin: 0; height: 250px; overflow: auto; background: #000; border: 1px solid #27272a; border-radius: 10px; padding: 10px; font: 12px/1.5 ui-monospace, Consolas, monospace; color: #d4d4d8; white-space: pre-wrap; }
  #error { color: #f87171; font-size: 13px; min-height: 18px; margin-top: 10px; }
  section { display: none; } section.on { display: block; }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tabs button.on { background: #3f3f46; border-color: #71717a; }
</style></head>
<body>
  <h1>Deploy to a Linux server</h1>

  <section id="s-where" class="on">
    <p>Installs Vein Server Manager on a Linux machine over SSH and adds it to your servers. The machine needs SSH turned on and a user who can use sudo.</p>
    <div class="row">
      <label>Server address<input type="text" id="host" placeholder="192.168.1.50 or vein.example.com" autocomplete="off"></label>
      <label>SSH port<input type="number" id="port" value="22" min="1" max="65535"></label>
    </div>
    <label>User name on the server<input type="text" id="user" placeholder="for example: ubuntu" autocomplete="off"></label>
    <div class="buttons"><button id="cancel1">Cancel</button><button class="primary" id="probe">Next</button></div>
  </section>

  <section id="s-key">
    <p>The server at <b id="k-server"></b> identifies itself with this key. Compare the fingerprint with the server's own (on the server: <span class="mono">ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</span>) if someone could be in between.</p>
    <div class="box"><div class="note" id="k-type"></div><div class="mono" id="k-print"></div></div>
    <div class="box bad" id="k-changed" style="display:none">This is NOT the key saved for this server when it was deployed. That happens when a server is reinstalled, and when something is impersonating it. Nothing more is sent to it. If you reinstalled the server, remove it under Servers first, then deploy again.</div>
    <div class="note" id="k-known" style="display:none">This matches the key saved for this server.</div>
    <div class="buttons"><button id="back1">Back</button><button class="primary" id="trust">This is my server</button></div>
  </section>

  <section id="s-auth">
    <h2>Sign in to the server</h2>
    <div class="tabs"><button data-m="password" class="on">Password</button><button data-m="key">Key file</button><button data-m="agent">SSH agent</button></div>
    <div id="m-password"><label>Password for <span class="who"></span><input type="password" id="password" autocomplete="off"></label></div>
    <div id="m-key" style="display:none">
      <div class="pick"><label>Private key file<input type="text" id="key_path" placeholder="C:\\Users\\you\\.ssh\\id_ed25519" autocomplete="off"></label><button id="browse" style="margin-bottom:10px">Browse</button></div>
      <label>Passphrase, if the key has one<input type="password" id="passphrase" autocomplete="off"></label>
    </div>
    <div id="m-agent" style="display:none"><p>Uses a key already loaded in the Windows OpenSSH agent.</p></div>
    <label id="sudo-row" style="display:none">Password for sudo (if different, or when signing in with a key)<input type="password" id="sudo_password" autocomplete="off"></label>
    <p class="note">Used for this installation only. Nothing typed here is saved.</p>
    <div class="buttons"><button id="back2">Back</button><button class="primary" id="check">Check the server</button></div>
  </section>

  <section id="s-choose">
    <h2 id="c-os"></h2>
    <div id="c-blocking"></div>
    <div id="c-notes"></div>
    <div id="c-kinds">
      <label class="choice" id="c-native"><input type="radio" name="kind" value="native"><span><b>As a service on the server</b><small>Lightest. Runs under its own account with systemd.</small><small class="why"></small></span></label>
      <label class="choice" id="c-docker"><input type="radio" name="kind" value="docker"><span><b>In Docker</b><small>Works on the most distributions; kept apart from the rest of the server.</small><small class="why"></small></span></label>
    </div>
    <label>Name for this server in your list<input type="text" id="name" maxlength="60"></label>
    <p class="note">The manager will listen on the server itself only. This app reaches it through SSH; the game ports are opened as usual.</p>
    <div class="buttons"><button id="back3">Back</button><button class="primary" id="install">Install</button></div>
  </section>

  <section id="s-run">
    <h2 id="r-step">Starting</h2>
    <pre id="r-log"></pre>
    <div id="r-done" style="display:none">
      <div class="box" id="r-pass" style="display:none; margin-top:12px">
        <div class="note">Admin password for this server. It is shown only now: copy it somewhere safe. You can change it later under System.</div>
        <div class="pick"><div class="mono" id="r-password"></div><button id="copy">Copy</button></div>
      </div>
      <p id="r-existing" style="display:none; margin-top:12px">The manager was updated. Sign in with this server's existing admin password.</p>
    </div>
    <div class="buttons"><button id="close" style="display:none">Close</button><button id="retry" style="display:none">Back</button><button class="primary" id="open" style="display:none">Open the server</button></div>
  </section>

  <div id="error" role="alert"></div>
<script>
  const api = () => window.pywebview.api;
  const $ = (id) => document.getElementById(id);
  const error = $('error');
  let method = 'password', timer = null;

  function step(name) { document.querySelectorAll('section').forEach((s) => s.classList.toggle('on', s.id === 's-' + name)); error.textContent = ''; }
  function busy(button, text) { const old = button.textContent; button.disabled = true; button.textContent = text; return () => { button.disabled = false; button.textContent = old; }; }
  function fail(result) { if (result && result.error) { error.textContent = result.error; return true; } error.textContent = ''; return false; }
  function box(parent, cls, text) { const d = document.createElement('div'); d.className = 'box ' + cls; d.textContent = text; parent.append(d); }

  $('probe').onclick = () => {
    const done = busy($('probe'), 'Contacting the server...');
    api().probe($('host').value, $('port').value, $('user').value).then((r) => {
      done();
      if (fail(r)) return;
      $('k-server').textContent = r.server; $('k-type').textContent = r.type; $('k-print').textContent = r.fingerprint;
      $('k-changed').style.display = r.changed ? '' : 'none'; $('k-known').style.display = r.known ? '' : 'none';
      $('trust').style.display = r.changed ? 'none' : '';
      document.querySelectorAll('.who').forEach((e) => e.textContent = r.server);
      if (!$('name').value) $('name').value = $('host').value;
      step('key');
    });
  };
  $('trust').onclick = () => step('auth');
  $('back1').onclick = () => step('where');
  $('back2').onclick = () => step('key');
  $('back3').onclick = () => step('auth');

  document.querySelectorAll('.tabs button').forEach((b) => b.onclick = () => {
    method = b.dataset.m;
    document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('on', x === b));
    ['password', 'key', 'agent'].forEach((m) => $('m-' + m).style.display = m === method ? '' : 'none');
    $('sudo-row').style.display = method === 'password' ? 'none' : '';
  });
  $('browse').onclick = () => api().browse_key().then((r) => { if (r.path) $('key_path').value = r.path; });

  $('check').onclick = () => {
    const done = busy($('check'), 'Checking...');
    const auth = { method, password: $('password').value, key_path: $('key_path').value, passphrase: $('passphrase').value, sudo_password: $('sudo_password').value };
    api().check(auth).then((r) => {
      done();
      if (fail(r)) { if (/sudo/.test(r.error)) $('sudo-row').style.display = ''; return; }
      $('c-os').textContent = r.os;
      $('c-blocking').replaceChildren(); $('c-notes').replaceChildren();
      r.blocking.forEach((t) => box($('c-blocking'), 'bad', t));
      r.notes.forEach((t) => box($('c-notes'), 'warn', t));
      ['native', 'docker'].forEach((k) => {
        const row = $('c-' + k), input = row.querySelector('input');
        input.disabled = !r[k].ok || r.blocking.length > 0; input.checked = r.recommended === k;
        row.classList.toggle('off', input.disabled); row.querySelector('.why').textContent = r[k].reason || '';
      });
      $('install').disabled = !r.recommended;
      $('install').textContent = r.existing ? 'Update' : 'Install';
      step('choose');
    });
  };

  $('install').onclick = () => {
    const kind = (document.querySelector('input[name=kind]:checked') || {}).value;
    api().install(kind, $('name').value).then((r) => {
      if (fail(r)) return;
      ['r-done', 'close', 'retry', 'open'].forEach((id) => $(id).style.display = 'none');
      $('r-log').textContent = ''; step('run'); poll();
    });
  };
  function poll() {
    api().status().then((s) => {
      $('r-step').textContent = s.state === 'failed' ? 'It did not finish' : s.step;
      const log = $('r-log'), bottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.textContent = s.lines.join('\\n'); if (bottom) log.scrollTop = log.scrollHeight;
      if (s.state === 'working') { timer = setTimeout(poll, 700); return; }
      if (s.state === 'failed') { error.textContent = s.error; $('retry').style.display = ''; $('close').style.display = ''; return; }
      if (s.state === 'done') {
        $('r-done').style.display = '';
        $('r-pass').style.display = s.password ? '' : 'none'; $('r-password').textContent = s.password || '';
        $('r-existing').style.display = s.password ? 'none' : '';
        $('open').style.display = ''; $('close').style.display = '';
      }
    });
  }
  $('copy').onclick = () => { navigator.clipboard.writeText($('r-password').textContent); $('copy').textContent = 'Copied'; };
  $('retry').onclick = () => step('auth');
  $('open').onclick = () => api().open();
  $('close').onclick = $('cancel1').onclick = () => api().close_window();

  window.addEventListener('pywebviewready', () => api().info().then((r) => {
    if (!r.available) { error.textContent = 'This copy of the app does not include the Linux package, so it cannot deploy. Use an installed or portable build.'; $('probe').disabled = true; }
  }));
</script>
</body></html>"""
