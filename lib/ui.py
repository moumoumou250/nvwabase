"""Local web panel for nvwa. Stdlib HTTP only, bound to loopback.

A random token is generated per launch and embedded in the page. API calls
must send it as ``X-Nvwa-Token`` or ``?token=``. Requests that carry a
foreign ``Origin`` are rejected, so another local page cannot drive the
panel just by knowing the port.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import fg  # noqa: E402
import runner  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_NAME_ACTION = re.compile(r"^/api/agents/([A-Za-z0-9_-]+)/(say|stop|stream)$")


PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nvwa</title>
<style>
  :root {
    --bg:#0a0e17; --panel:#111726; --panel2:#0e1422; --line:#1e2740;
    --txt:#e6edf7; --dim:#8a97b3; --teal:#10b981; --amber:#f59e0b;
    --rose:#f43f5e; --blue:#3b82f6;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--txt);
    font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  body { display: grid; grid-template-rows: auto 1fr 46vh; height: 100vh; }
  header { display: flex; gap: 16px; align-items: center; padding: 12px 16px;
    border-bottom: 1px solid var(--line); background: #10182a; }
  header b { letter-spacing: .4px; }
  header span { color: var(--dim); font-size: 12px; }
  main { display: grid; grid-template-columns: 280px 1fr; min-height: 0; }
  aside, section { min-height: 0; }
  aside { border-right: 1px solid var(--line); padding: 12px; overflow: auto; background: var(--panel); }
  form { display: grid; gap: 6px; margin-bottom: 12px; }
  input, button { font: inherit; color: var(--txt); background: var(--panel2);
    border: 1px solid var(--line); border-radius: 7px; padding: 6px 8px; }
  button { cursor: pointer; }
  button:hover { border-color: #2f3a5a; }
  button.danger { color: #fda4af; }
  .agent { display: grid; grid-template-columns: 1fr auto; gap: 4px 8px;
    padding: 8px; border: 1px solid transparent; border-radius: 8px; cursor: pointer; }
  .agent.selected { border-color: #2f3a5a; background: #161e33; }
  .agent .meta { grid-column: 1 / -1; color: var(--dim); font-size: 11px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--dim); }
  .dot.running, .dot.starting { background: var(--teal); }
  .dot.stopped, .dot.exited { background: var(--rose); }
  section { display: grid; grid-template-rows: 1fr auto; min-width: 0; }
  #log { margin: 0; padding: 12px 14px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
  .composer { display: flex; gap: 8px; padding: 8px; border-top: 1px solid var(--line); }
  .composer input { flex: 1; }
  #graph { width: 100%; height: 100%; border: 0; border-top: 1px solid var(--line); background: #0a0e17; }
  #err { color: var(--amber); min-height: 1.2em; font-size: 12px; }
  @media (max-width: 800px) {
    body { grid-template-rows: auto auto 50vh 46vh; }
    main { grid-template-columns: 1fr; }
    aside { border-right: 0; border-bottom: 1px solid var(--line); max-height: 40vh; }
  }
</style>
</head>
<body>
<header>
  <b>nvwa</b>
  <span id="sub">local panel</span>
</header>
<main>
  <aside>
    <form id="spawn">
      <input name="name" placeholder="name (optional)" autocomplete="off">
      <input name="mission" placeholder="mission" autocomplete="off">
      <input name="agent" placeholder="command (default: FG_BACKEND)" autocomplete="off">
      <button type="submit">start</button>
    </form>
    <div id="err"></div>
    <div id="agents"></div>
  </aside>
  <section>
    <pre id="log">select an agent</pre>
    <form class="composer" id="say">
      <input name="text" placeholder="say…" autocomplete="off">
      <button type="submit">send</button>
    </form>
  </section>
</main>
<iframe id="graph" title="fact graph" src="/graph"></iframe>
<script>
const TOKEN = "__TOKEN__";
const headers = {"X-Nvwa-Token": TOKEN, "Content-Type": "application/json"};
let selected = null;
let source = null;
const logEl = document.getElementById("log");
const errEl = document.getElementById("err");

function showError(text) { errEl.textContent = text || ""; }

async function api(path, options) {
  const res = await fetch(path, Object.assign({headers}, options || {}));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function renderAgents(agents) {
  const box = document.getElementById("agents");
  box.replaceChildren();
  if (!agents.length) {
    const empty = document.createElement("div");
    empty.className = "meta";
    empty.textContent = "no agents";
    box.appendChild(empty);
    return;
  }
  for (const agent of agents) {
    const row = document.createElement("div");
    row.className = "agent" + (agent.name === selected ? " selected" : "");
    const title = document.createElement("div");
    const dot = document.createElement("i");
    dot.className = "dot " + (agent.status || "");
    title.appendChild(dot);
    title.appendChild(document.createTextNode(agent.name || ""));
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "danger";
    stop.textContent = "stop";
    stop.addEventListener("click", (event) => {
      event.stopPropagation();
      api("/api/agents/" + encodeURIComponent(agent.name) + "/stop", {method: "POST", body: "{}"})
        .then(refresh).catch((err) => showError(err.message));
    });
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [agent.status, agent.runner, agent.backend].filter(Boolean).join(" · ");
    row.appendChild(title);
    row.appendChild(stop);
    row.appendChild(meta);
    row.addEventListener("click", () => select(agent.name));
    box.appendChild(row);
  }
}

async function refresh() {
  try {
    const body = await api("/api/agents");
    renderAgents(body.agents || []);
    showError("");
  } catch (err) {
    showError(err.message);
  }
}

function select(name) {
  selected = name;
  logEl.textContent = "";
  if (source) source.close();
  source = new EventSource("/api/agents/" + encodeURIComponent(name) + "/stream?token=" + encodeURIComponent(TOKEN));
  source.onmessage = (event) => {
    let line = event.data;
    try { line = JSON.parse(event.data); } catch (err) { /* plain text */ }
    logEl.textContent += line + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  };
  source.onerror = () => { /* browser retries */ };
  refresh();
}

document.getElementById("spawn").addEventListener("submit", (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  api("/api/agents", {method: "POST", body: JSON.stringify(data)})
    .then((body) => { if (body.agent) select(body.agent.name); return refresh(); })
    .catch((err) => showError(err.message));
});

document.getElementById("say").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!selected) { showError("select an agent"); return; }
  const text = new FormData(event.target).get("text") || "";
  api("/api/agents/" + encodeURIComponent(selected) + "/say", {
    method: "POST",
    body: JSON.stringify({text}),
  }).then(() => { event.target.reset(); }).catch((err) => showError(err.message));
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Panel:
    def __init__(self, store: Path):
        self.store = Path(store)
        self.token = os.urandom(16).hex()
        self.closing = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT

    def serve(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
        self.host = host
        handler = _handler_factory(self)
        try:
            self.httpd = ThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            raise runner.NvwaError(f"could not bind {host}:{port}: {exc}") from exc
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        url = f"http://{self.host}:{self.port}/"
        print(f"[nvwa] ui {url}")
        if host not in ("127.0.0.1", "localhost", "::1"):
            print("[nvwa] WARNING: bound outside loopback — the token is the only gate.")
        print("[nvwa] Ctrl-C to stop.")
        if open_browser:
            threading.Timer(0.3, fg._try_open_url, args=(url,)).start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[nvwa] ui stopped.")
        finally:
            self.close()
        return 0

    def serve_background(self, host: str = DEFAULT_HOST, port: int = 0) -> str:
        """Start on a background thread. Returns the base URL. ``port=0`` picks a free port."""
        self.host = host
        handler = _handler_factory(self)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, name="nvwa-ui", daemon=True).start()
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        self.closing.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def authorized(self, handler: BaseHTTPRequestHandler, query: dict) -> bool:
        supplied = handler.headers.get("X-Nvwa-Token") or ""
        if not supplied:
            values = query.get("token") or []
            supplied = values[0] if values else ""
        if not supplied or not _same(supplied, self.token):
            return False
        origin = handler.headers.get("Origin")
        if origin and origin not in _allowed_origins(self.host, self.port):
            return False
        return True

    def graph(self) -> dict:
        store = fg.Store(self.store)
        if not store.exists():
            return {
                "project": {
                    "id": "proj",
                    "status": "active",
                    "origin": "",
                    "goal": "(no graph yet — fgc init --goal \"...\")",
                },
                "facts": [],
                "intents": [],
                "hints": [],
            }
        with store.locked():
            return fg.build_graph_view(store)

    def graph_html(self) -> str:
        html = fg._load_view_template()
        poll = f"/api/graph?token={self.token}"
        html = html.replace('"__POLL_URL__"', json.dumps(poll))
        # The shared view starts paused. The panel turns live refresh on.
        html = html.replace(
            "refresh();\n</script>",
            "refresh();\ndocument.getElementById('btn-autorefresh').click();\n</script>",
        )
        return html


def _same(left: str, right: str) -> bool:
    import hmac
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _allowed_origins(host: str, port: int) -> set[str]:
    hosts = {host}
    if host in ("127.0.0.1", "localhost", "::1"):
        hosts.update({"127.0.0.1", "localhost"})
    return {f"http://{item}:{port}" for item in hosts}


def _handler_factory(panel: Panel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def _route(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if method == "GET" and path in ("/", "/index.html"):
                    self._html(PANEL_HTML.replace("__TOKEN__", panel.token))
                    return
                if method == "GET" and path == "/graph":
                    self._html(panel.graph_html())
                    return
                if not panel.authorized(self, query):
                    origin = self.headers.get("Origin")
                    if origin and origin not in _allowed_origins(panel.host, panel.port):
                        self._json(403, {"error": "cross-origin request rejected"})
                    else:
                        self._json(401, {"error": "unauthorized"})
                    return
                if method == "GET" and path == "/api/agents":
                    agents = [runner.public_agent(item) for item in runner.list_agents(panel.store)]
                    self._json(200, {"agents": agents})
                    return
                if method == "GET" and path == "/api/graph":
                    self._json(200, panel.graph())
                    return
                match = _NAME_ACTION.fullmatch(path)
                if match and method == "GET" and match.group(2) == "stream":
                    self._stream(match.group(1))
                    return
                if match and method == "POST" and match.group(2) == "say":
                    body = self._body()
                    text = body.get("text") if isinstance(body, dict) else None
                    if not isinstance(text, str):
                        self._json(400, {"error": "text must be a string"})
                        return
                    runner.say(panel.store, match.group(1), text)
                    self._json(200, {"ok": True})
                    return
                if match and method == "POST" and match.group(2) == "stop":
                    state = runner.stop_agent(panel.store, match.group(1))
                    self._json(200, {"ok": True, "agent": runner.public_agent(state)})
                    return
                if method == "POST" and path == "/api/agents":
                    body = self._body()
                    if not isinstance(body, dict):
                        self._json(400, {"error": "JSON object required"})
                        return
                    state = runner.spawn_agent(
                        panel.store,
                        name=(body.get("name") or None),
                        agent=(body.get("agent") or None),
                        argv=body.get("argv") if isinstance(body.get("argv"), list) else None,
                        mission=str(body.get("mission") or ""),
                    )
                    self._json(200, {"ok": True, "agent": runner.public_agent(state)})
                    return
                self._json(404, {"error": "not found"})
            except runner.NvwaError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        def _body(self):
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 1_000_000:
                raise runner.NvwaError("request body is too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise runner.NvwaError(f"invalid JSON: {exc}") from exc

        def _html(self, text: str) -> None:
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _stream(self, name: str) -> None:
            home = runner.AgentPaths(panel.store, name)
            runner.load_state(panel.store, name)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            offset = 0
            if home.log.exists():
                size = home.log.stat().st_size
                offset = max(0, size - 65536)
            last_pane = None
            try:
                while not panel.closing.is_set():
                    state = runner.load_state(panel.store, name)
                    if state.get("runner") == "tmux" and state.get("status") == "running":
                        pane = runner._tmux_capture(state.get("session") or "nvwa", name)
                        if pane != last_pane:
                            self._sse(pane)
                            last_pane = pane
                    else:
                        chunk, offset = _read_log(home.log, offset)
                        if chunk:
                            self._sse(chunk)
                        else:
                            self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if panel.closing.wait(0.25):
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _sse(self, text: str) -> None:
            if not text:
                return
            lines = text.splitlines()
            if not lines:
                return
            for line in lines:
                payload = json.dumps(line, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))

    return Handler


def _read_log(path: Path, offset: int) -> tuple[str, int]:
    if not path.exists():
        return "", offset
    size = path.stat().st_size
    if size < offset:
        offset = 0
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    return data.decode("utf-8", errors="replace"), offset + len(data)
