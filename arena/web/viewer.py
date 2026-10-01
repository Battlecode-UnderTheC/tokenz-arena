"""The unswbc replay viewer in a plain browser page.

The toolkit ships its VS Code replay viewer (`unswbc/replay-viewer.vsix`). Its webview is a
self-contained bundle (`dist/webview/webview.{js,css}`) whose only tie to VS Code is
`acquireVsCodeApi().postMessage({type: "ready"})`. The host answers with
`{type: "open", name, url}` and the bundle fetches the url. The page below stubs that API and
supplies the theme variables the CSS reads. The bundle is extracted from the installed
toolkit at start-up, never copied into this repo, so it always matches the pinned judge.
"""

from __future__ import annotations

import json
import zipfile
from html import escape
from pathlib import Path

import unswbc

VSIX = Path(unswbc.__file__).parent / "replay-viewer.vsix"
_PREFIX = "extension/dist/webview/"

# VS Code's dark theme colours, for the variables webview.css reads.
_THEME = """
  --vscode-font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  --vscode-editor-font-family: ui-monospace, "SF Mono", Menlo, monospace;
  --vscode-font-size: 13px;
  --vscode-foreground: #cccccc;
  --vscode-descriptionForeground: #9d9d9d;
  --vscode-disabledForeground: #6b6b6b;
  --vscode-errorForeground: #f48771;
  --vscode-focusBorder: #0078d4;
  --vscode-editor-background: #1e1e1e;
  --vscode-sideBar-background: #181818;
  --vscode-panel-border: #2b2b2b;
  --vscode-widget-border: #313131;
  --vscode-input-background: #313131;
  --vscode-button-background: #0078d4;
  --vscode-button-foreground: #ffffff;
  --vscode-button-hoverBackground: #026ec1;
  --vscode-button-secondaryBackground: #313131;
  --vscode-button-secondaryForeground: #cccccc;
  --vscode-button-secondaryHoverBackground: #3c3c3c;
  --vscode-toolbar-activeBackground: #3c3c3c;
  --vscode-toolbar-hoverBackground: #2a2a2a;
  --vscode-charts-blue: #3794ff;
  --vscode-charts-orange: #d18616;
  --vscode-charts-red: #f14c4c;
  --vscode-charts-yellow: #cca700;
  --vscode-charts-green: #89d185;
"""


# One hook patched into the bundle: right after the board's dragons are drawn, call
# window.__arenaAfterDragons(ctx, views, layout) inside the same transform, so the page can draw
# overlays (the queen marker) in board pixels. If a new toolkit's bundle doesn't contain the
# target, the bundle is served unpatched and the overlay just doesn't appear.
_PATCH_VERSION = "queen-1"
_PATCH_TARGET = "for(let e of m)Oo(this.#r,e.team).drawDragon(u,e,l);if(u.restore(),"
_PATCH_HOOK = ("for(let e of m)Oo(this.#r,e.team).drawDragon(u,e,l);"
               "window.__arenaAfterDragons&&window.__arenaAfterDragons(u,m,l);if(u.restore(),")


def extract(dest: Path) -> Path:
    """Unpacks webview.js (patched with the overlay hook) and webview.css into dest, once per
    toolkit version."""
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dest / ".source"
    source = f"{VSIX.stat().st_size}:{VSIX.stat().st_mtime_ns}:{_PATCH_VERSION}"
    if stamp.exists() and stamp.read_text() == source:
        return dest
    with zipfile.ZipFile(VSIX) as z:
        css = z.read(_PREFIX + "webview.css")
        js = z.read(_PREFIX + "webview.js").decode("utf-8")
    if js.count(_PATCH_TARGET) == 1:
        js = js.replace(_PATCH_TARGET, _PATCH_HOOK)
    (dest / "webview.css").write_bytes(css)
    (dest / "webview.js").write_text(js, encoding="utf-8")
    stamp.write_text(source)
    return dest


# The queen (dragon id 0 for one team, 1 for the other: the viewer's own rule) gets a gold
# ring around its head and a crown, toggled with the button (or the Q key) and remembered.
_QUEEN_OVERLAY = """<script>
(() => {
  let on = true;
  try { on = localStorage.getItem("arena.queens") !== "off"; } catch (e) {}
  window.__arenaAfterDragons = (ctx, views, layout) => {
    if (!on) return;
    const c = layout.cell;
    for (const v of views) {
      if (v.id > 1 || v.ghost || !(v.alpha > 0) || !v.head) continue;
      ctx.save();
      ctx.globalAlpha = Math.min(1, v.alpha);
      ctx.strokeStyle = "#ffd23f"; ctx.lineWidth = Math.max(1.5, c * 0.14);
      ctx.shadowColor = "rgba(0,0,0,.6)"; ctx.shadowBlur = c * 0.3;
      ctx.beginPath(); ctx.arc(v.head.x, v.head.y, c * 0.85, 0, Math.PI * 2); ctx.stroke();
      ctx.font = `${Math.max(9, c * 1.05)}px system-ui, sans-serif`;
      ctx.textAlign = "center"; ctx.textBaseline = "bottom"; ctx.fillStyle = "#ffd23f";
      ctx.fillText("\u265B", v.head.x, v.head.y - c * 0.8);
      ctx.restore();
    }
  };
  const btn = document.createElement("button");
  const paint = () => { btn.textContent = "\u265B queens: " + (on ? "on" : "off"); btn.style.opacity = on ? 1 : .6; };
  const toggle = () => { on = !on; paint(); try { localStorage.setItem("arena.queens", on ? "on" : "off"); } catch (e) {}
    window.dispatchEvent(new Event("resize")); };
  btn.title = "highlight each team's queen (Q)";
  btn.style.cssText = "position:fixed;right:22px;top:calc(var(--arena-header, 0px) + 22px);z-index:9;padding:4px 10px;border-radius:6px;" +
    "border:1px solid #ffd23f;background:#1e1e1e;color:#ffd23f;font:12px system-ui;cursor:pointer";
  btn.onclick = toggle; paint();
  document.addEventListener("keydown", (e) => { if ((e.key === "q" || e.key === "Q") && !e.ctrlKey && !e.metaKey
    && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "")) toggle(); });
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(btn));
})();
</script>"""


def _js(value) -> str:
    """JSON safe to put inside a <script> element."""
    return json.dumps(value).replace("</", "<\\/")


def page(replay_url: str, name: str, assets: str = "/viewer", header: str = "",
         head: str = "", team_names: dict | None = None) -> str:
    """The viewer page for one replay. `assets` is where webview.js and .css are served.
    `header` is HTML for a bar above the viewer (the website's game page) and `head` extra
    markup for <head> (its styles). `team_names` ({"A": ..., "B": ...}) relabels the viewer's
    "Team A" / "Team B" with real names (the bundle only knows seats)."""
    if team_names:
        head += _relabel(team_names)
    head += _QUEEN_OVERLAY
    return _shell(name, assets, "replay",
                  _js({"type": "open", "name": name, "url": replay_url}), header, head)


def _relabel(names: dict) -> str:
    """Rewrites "Team A"/"Team B" in the viewer's text as it renders (a MutationObserver over
    text nodes), keeping the seat letter: "Team A" -> "us · v12 (A)"."""
    pairs = {f"Team {k}": f"{v} ({k})" for k, v in names.items() if v}
    return f"""<script>
(() => {{
  const pairs = {_js(pairs)};
  const re = /Team [AB]/g;
  const fix = (n) => {{
    if (n.nodeType === 3) {{
      if (re.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(re, (m) => pairs[m] || m);
      re.lastIndex = 0;
    }} else if (n.nodeType === 1 && n.tagName !== "SCRIPT") {{
      for (const c of n.childNodes) fix(c);
    }}
  }};
  new MutationObserver((ms) => ms.forEach((m) => {{
    if (m.type === "characterData") fix(m.target); else m.addedNodes.forEach(fix);
  }})).observe(document.documentElement, {{subtree: true, childList: true, characterData: true}});
  document.addEventListener("DOMContentLoaded", () => fix(document.body));
}})();
</script>"""


def map_page(text: str, name: str, assets: str = "/viewer", header: str = "",
             head: str = "") -> str:
    """The bundle's map view (the toolkit's map editor) showing one map, for map previews."""
    return _shell(name, assets, "map", _js({"type": "map", "text": text}), header, head)


def _shell(name: str, assets: str, view: str, reply: str, header: str, head: str) -> str:
    """The page around the bundle: answers its {type: "ready"} with `reply`."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(name)}</title>
<link rel="stylesheet" href="{assets}/webview.css">
<style>
:root {{{_THEME}}}
html, body {{ margin: 0; height: 100%; background: var(--vscode-editor-background);
  color: var(--vscode-foreground); font-family: var(--vscode-font-family); }}
body {{ display: flex; flex-direction: column; }}
#app {{ flex: 1; min-height: 0; }}
</style>
{head}
<script>
window.acquireVsCodeApi = () => ({{
  postMessage(m) {{ if (m && m.type === "ready") window.postMessage({reply}, "*"); }},
  getState() {{ return undefined; }},
  setState() {{}},
}});
</script>
</head>
<body>{header}<div id="app" data-view="{view}"></div><script src="{assets}/webview.js"></script></body>
</html>
"""
