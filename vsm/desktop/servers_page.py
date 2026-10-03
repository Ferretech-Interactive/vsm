"""The small "Servers" window. It is local HTML with its own Python bridge.

The bridge is attached to this window only. The main window, which shows pages served by
managers (possibly on other machines), has no bridge at all.
"""

PLACEHOLDER_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#09090b;color:#a1a1aa;
font:15px system-ui,Segoe UI,sans-serif;text-align:center}b{color:#fafafa;display:block;font-size:18px;margin-bottom:8px}
</style></head><body><div><b>No server selected</b>Choose Servers &gt; Manage servers to add one.</div></body></html>"""

SERVERS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Servers</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 20px; background: #09090b; color: #e4e4e7; font: 14px system-ui, "Segoe UI", sans-serif; }
  h1 { font-size: 18px; margin: 0 0 4px; color: #fafafa; }
  p { color: #a1a1aa; margin: 0 0 16px; font-size: 13px; }
  ul { list-style: none; margin: 0 0 20px; padding: 0; border: 1px solid #27272a; border-radius: 10px; }
  li { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-top: 1px solid #27272a; }
  li:first-child { border-top: 0; }
  .name { flex: 1; min-width: 0; }
  .name b { display: block; color: #fafafa; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name span { color: #71717a; font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
  .tag { font-size: 11px; padding: 2px 7px; border-radius: 6px; background: #064e3b; color: #6ee7b7; }
  .warn { color: #fbbf24; font-size: 12px; display: block; }
  button { font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid #3f3f46; background: #27272a; color: #f4f4f5; cursor: pointer; }
  button:hover { background: #3f3f46; }
  button.primary { background: #059669; border-color: #10b981; color: #fff; }
  button.link { background: none; border-color: transparent; color: #a1a1aa; padding: 6px; }
  form { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: end; }
  label { font-size: 12px; color: #d4d4d8; display: block; }
  input { width: 100%; margin-top: 4px; padding: 7px 10px; border-radius: 8px; border: 1px solid #3f3f46; background: #09090b; color: #fafafa; font: inherit; }
  input:focus { outline: none; border-color: #10b981; }
  #error { color: #f87171; font-size: 13px; min-height: 18px; margin-top: 8px; }
</style></head>
<body>
  <h1>Servers</h1>
  <p>Vein Server Manager installations you can open from this window. You sign in to each with that server's admin password.</p>
  <ul id="list"></ul>
  <form id="add">
    <label>Name<input id="name" maxlength="60" placeholder="Weekend server" required></label>
    <label>Address<input id="address" placeholder="192.168.1.20 or vein.example.com" required></label>
    <button class="primary" type="submit">Add</button>
  </form>
  <div id="error" role="alert"></div>
  <p style="margin-top:14px">No manager on the machine yet? <button id="deploy">Deploy to a Linux server...</button></p>
<script>
  const api = () => window.pywebview.api;
  const list = document.getElementById('list');
  document.getElementById('deploy').onclick = () => api().deploy();
  const error = document.getElementById('error');

  function render(servers) {
    list.replaceChildren(...servers.map((s) => {
      const li = document.createElement('li');
      const name = document.createElement('div'); name.className = 'name';
      const title = document.createElement('b'); title.textContent = s.name;
      const url = document.createElement('span'); url.textContent = s.detail || s.url;
      name.append(title, url);
      if (s.warning) { const w = document.createElement('span'); w.className = 'warn'; w.textContent = s.warning; name.append(w); }
      li.append(name);
      if (s.current) { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = 'Open'; li.append(tag); }
      const open = document.createElement('button'); open.textContent = 'Connect';
      open.onclick = () => api().connect(s.id).then(show);
      li.append(open);
      if (!s.local) {
        const remove = document.createElement('button'); remove.className = 'link'; remove.textContent = 'Remove';
        remove.onclick = () => { if (confirm('Remove ' + s.name + ' from the list?' + (s.ssh ? ' The manager stays installed on that machine; only this app forgets it and its key.' : ''))) api().remove(s.id).then(show); };
        li.append(remove);
      }
      return li;
    }));
    if (!servers.length) { const li = document.createElement('li'); li.textContent = 'No servers yet. Add one below.'; list.append(li); }
  }
  function show(result) { error.textContent = result.error || ''; render(result.servers); }

  document.getElementById('add').addEventListener('submit', (e) => {
    e.preventDefault();
    api().add(document.getElementById('name').value, document.getElementById('address').value).then((result) => {
      show(result);
      if (!result.error) e.target.reset();
    });
  });
  window.addEventListener('pywebviewready', () => api().list().then(show));
</script>
</body></html>"""
