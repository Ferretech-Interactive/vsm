"""The small "About" window: local HTML with a bridge that can do exactly three harmless things.

Like the Servers window, the bridge belongs to this window only. It takes no addresses from the
page: links are named, and the names map to addresses fixed here.
"""
from __future__ import annotations

import base64
import html
from pathlib import Path

from ..core.updates import REPO_URL

LINKS = {
    "project": REPO_URL,
    "releases": REPO_URL + "/releases",
    "license": REPO_URL + "/blob/main/LICENSE",
    "publisher": "https://ferretech.org",
    "original": "https://github.com/Wolverinex1974/Vein-Server-Manager",
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>About Vein Server Manager</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 28px 28px 20px; background: #09090b; color: #e4e4e7; font: 14px system-ui, "Segoe UI", sans-serif; text-align: center; }
  img { width: 72px; height: 72px; border-radius: 14px; }
  h1 { font-size: 20px; margin: 14px 0 2px; color: #fafafa; font-weight: 600; }
  .version { color: #a1a1aa; font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
  .kind { display: inline-block; margin-top: 10px; font-size: 12px; padding: 3px 9px; border-radius: 6px; background: #18181b; border: 1px solid #27272a; color: #d4d4d8; }
  p { color: #a1a1aa; margin: 18px 0 0; font-size: 13px; line-height: 1.55; }
  a { color: #7dd3fc; cursor: pointer; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .soon { color: #71717a; cursor: default; }
  .legal { margin-top: 16px; font-size: 11px; line-height: 1.5; color: #71717a; }
  .links { margin-top: 18px; padding-top: 16px; border-top: 1px solid #27272a; display: flex; justify-content: center; gap: 18px; font-size: 13px; }
  .buttons { margin-top: 20px; display: flex; justify-content: center; gap: 8px; }
  button { font: inherit; padding: 6px 16px; border-radius: 8px; border: 1px solid #3f3f46; background: #27272a; color: #f4f4f5; cursor: pointer; }
  button:hover { background: #3f3f46; }
  button.primary { background: #059669; border-color: #10b981; color: #fff; }
</style></head>
<body>
  <img src="__LOGO__" alt="">
  <h1>Vein Server Manager</h1>
  <div class="version">Version __VERSION__</div>
  <div class="kind">__KIND__</div>
  <p>Published by <a data-link="publisher">FerreTech Interactive</a>.<br>
     Based on the original Vein Server Manager by <a data-link="original">Wolverinex1974</a>.</p>
  <div class="links">
    <a data-link="project">GitHub</a>
    <a data-link="releases">Releases</a>
    <a data-link="license">MIT license</a>
    <span class="soon" title="Coming soon">Discord</span>
  </div>
  <p class="legal">An independent project, not affiliated with, endorsed by or connected to VEIN or
     Ramjet Studios. All trademarks and copyrights belong to their respective owners.</p>
  <div class="buttons">
    __UPDATE__
    <button class="primary" id="close">Close</button>
  </div>
<script>
  const api = () => window.pywebview.api;
  document.querySelectorAll("[data-link]").forEach((a) => a.addEventListener("click", () => api().open(a.dataset.link)));
  document.getElementById("close").addEventListener("click", () => api().close());
  const update = document.getElementById("update");
  if (update) update.addEventListener("click", () => api().check_for_updates());
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") api().close(); });
</script>
</body></html>"""


def about_html(version: str, kind: str, icon: Path, can_update: bool) -> str:
    try:
        logo = "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode("ascii")
    except OSError:
        logo = ""
    return (
        _PAGE.replace("__LOGO__", logo)
        .replace("__VERSION__", html.escape(version))
        .replace("__KIND__", html.escape(kind))
        .replace("__UPDATE__", '<button id="update">Check for updates</button>' if can_update else "")
    )
