"""Agent runners for nvwa.

The default runner (``NVWA_RUNNER=subprocess``) starts the backend CLI as a
child process. A detached supervisor copies its stdout into
``.fg/agents/<name>.log`` and feeds ``say`` messages from
``.fg/agents/<name>.inbox`` into the child's stdin. State lives in
``.fg/agents/<name>.json``.

``NVWA_RUNNER=tmux`` keeps the previous pane-per-agent behavior. It needs
a tmux binary and is not available on native Windows.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import backends  # noqa: E402
import locking  # noqa: E402

NAME_RE_TEXT = r"^[A-Za-z0-9_-]+$"
NAME_RE = re.compile(NAME_RE_TEXT)

_log_locks: dict[str, threading.Lock] = {}
_log_locks_guard = threading.Lock()


class NvwaError(Exception):
    """User-facing nvwa failure."""


class AgentPaths:
    def __init__(self, store: Path, name: str):
        validate_name(name)
        self.store = Path(store)
        self.name = name
        self.dir = self.store / "agents"
        self.state_path = self.dir / f"{name}.json"
        self.log = self.dir / f"{name}.log"
        self.inbox = self.dir / f"{name}.inbox"
        self.stop = self.dir / f"{name}.stop"
        self.lock = self.dir / ".lock"


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise NvwaError(f"agent name must match {NAME_RE_TEXT}")
    return name


def resolve_runner(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get("NVWA_RUNNER") or "subprocess").strip().lower()
    if raw in ("subprocess", "proc", "process"):
        return "subprocess"
    if raw == "tmux":
        return "tmux"
    raise NvwaError(f"unknown runner {raw!r}; use subprocess or tmux")


def split_command(text: str) -> list[str]:
    """Split an ``--agent`` command.

    Unix uses :func:`shlex.split`. Windows paths treat ``\\`` as a separator,
    so quoting there is double quotes only and backslashes are kept.
    """
    if os.name != "nt":
        return shlex.split(text, posix=True)
    parts: list[str] = []
    buf: list[str] = []
    quote = False
    for ch in text:
        if ch == '"':
            quote = not quote
            continue
        if ch.isspace() and not quote:
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if quote:
        raise NvwaError("unbalanced quotes in --agent")
    if buf:
        parts.append("".join(buf))
    return parts


def default_backend_argv(agent: str | None = None) -> tuple[str, list[str]]:
    """Return ``(backend_label, argv)`` for a spawn.

    ``agent`` is a command string (``--agent``). When it is omitted the
    command is the CLI selected by ``FG_BACKEND`` (claude, codex, or grok).
    """
    if agent is not None and agent.strip():
        parts = split_command(agent)
        if not parts:
            raise NvwaError("--agent is empty")
        return Path(parts[0]).name, parts
    try:
        backend = backends.resolve_backend(None)
        binary = backends.resolve_binary(backend)
    except backends.BackendError as exc:
        raise NvwaError(str(exc)) from exc
    return backend, [binary]


def env_for(backend_label: str, argv: list[str]) -> dict:
    """Child environment. API keys are copied the same way dispatch copies them."""
    candidates = [backend_label.lower(), Path(argv[0]).name.lower()]
    for name in candidates:
        stem = name[:-4] if name.endswith(".exe") else name
        if stem in ("claude", "codex", "grok"):
            env = backends.child_env(stem)
            break
    else:
        env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def agents_dir(store: Path) -> Path:
    path = Path(store) / "agents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _lock(store: Path):
    return locking.file_lock(Path(store) / "agents" / ".lock")


def load_state(store: Path, name: str) -> dict:
    path = AgentPaths(store, name).state_path
    if not path.exists():
        raise NvwaError(f"no such agent {name!r}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NvwaError(f"cannot read state for {name}: {exc}") from exc
    if not isinstance(data, dict):
        raise NvwaError(f"invalid state for {name}")
    return data


def update_state(store: Path, name: str, **fields) -> dict:
    with _lock(store):
        state = load_state(store, name)
        state.update(fields)
        _atomic_write(
            AgentPaths(store, name).state_path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )
        return state


def _write_new_state(store: Path, state: dict) -> None:
    name = state["name"]
    with _lock(store):
        _atomic_write(
            AgentPaths(store, name).state_path,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )


def append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    with _log_locks_guard:
        lock = _log_locks.setdefault(key, threading.Lock())
    with lock:
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    # Reap a zombie that belongs to this process. kill(pid, 0) is true for
    # zombies, which would make stop() wait out its whole deadline.
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_pid(pid: int | None) -> None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return


def max_workers() -> int:
    raw = os.environ.get("NVWA_MAX_WORKERS", "8")
    try:
        value = int(raw)
    except ValueError:
        return 8
    return value if value >= 1 else 8


def _running_count(store: Path) -> int:
    return sum(1 for agent in list_agents(store) if agent.get("status") == "running")


def _next_name(store: Path) -> str:
    existing = {a["name"] for a in list_agents(store)}
    n = 1
    while f"w{n}" in existing:
        n += 1
    return f"w{n}"


def list_agents(store: Path) -> list[dict]:
    directory = Path(store) / "agents"
    if not directory.exists():
        return []
    agents = []
    for path in sorted(directory.glob("*.json")):
        if not NAME_RE.match(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("name"):
            agents.append(_refresh(Path(store), data))
    return agents


def _refresh(store: Path, state: dict) -> dict:
    name = state.get("name")
    if not isinstance(name, str):
        return state
    status = state.get("status")
    runner = state.get("runner")
    if status != "running":
        return state
    if runner == "subprocess":
        sup = state.get("supervisor_pid")
        child = state.get("pid")
        if sup and not pid_alive(sup) and not pid_alive(child):
            try:
                return update_state(store, name, status="exited")
            except NvwaError:
                state["status"] = "exited"
                return state
    elif runner == "tmux":
        if not _tmux_window_exists(state.get("session") or "nvwa", name):
            try:
                return update_state(store, name, status="exited")
            except NvwaError:
                state["status"] = "exited"
                return state
    return state


def public_agent(state: dict) -> dict:
    keys = (
        "name", "status", "backend", "runner", "pid", "supervisor_pid",
        "started_at", "mission", "exit_code", "argv", "session",
    )
    out = {key: state.get(key) for key in keys if key in state or key in ("name", "status")}
    out["name"] = state.get("name")
    out["status"] = state.get("status")
    return out


def format_ps(agents: list[dict]) -> str:
    if not agents:
        return "(no agents)"
    lines = [f"{'NAME':<14} {'STATUS':<10} {'RUNNER':<12} {'BACKEND':<16} PID"]
    for agent in agents:
        pid = agent.get("pid") or ""
        lines.append(
            f"{str(agent.get('name') or ''):<14} "
            f"{str(agent.get('status') or ''):<10} "
            f"{str(agent.get('runner') or ''):<12} "
            f"{str(agent.get('backend') or ''):<16} "
            f"{pid}"
        )
    return "\n".join(lines)


def spawn_agent(
    store: Path,
    *,
    name: str | None = None,
    agent: str | None = None,
    argv: list[str] | None = None,
    mission: str = "",
    runner: str | None = None,
    session: str | None = None,
) -> dict:
    store = Path(store)
    kind = resolve_runner(runner)
    if argv is not None:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise NvwaError("argv must be a list of non-empty strings")
        backend = Path(argv[0]).name
        command = list(argv)
    else:
        backend, command = default_backend_argv(agent)
    if name is None or not str(name).strip():
        name = _next_name(store)
    validate_name(name)
    if _running_count(store) >= max_workers():
        raise NvwaError(f"CAP: {max_workers()} agents — stop one first")
    home = AgentPaths(store, name)
    if home.state_path.exists():
        current = load_state(store, name)
        if current.get("status") == "running":
            refreshed = _refresh(store, current)
            if refreshed.get("status") == "running":
                raise NvwaError(f"agent {name!r} is already running")
    agents_dir(store)
    home.inbox.mkdir(parents=True, exist_ok=True)
    for stale in home.inbox.glob("*"):
        if stale.is_file():
            stale.unlink()
    if home.stop.exists():
        home.stop.unlink()
    home.log.write_text("", encoding="utf-8")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state = {
        "name": name,
        "runner": kind,
        "backend": backend,
        "argv": command,
        "mission": mission or "",
        "status": "starting",
        "pid": None,
        "supervisor_pid": None,
        "started_at": now,
        "exit_code": None,
        "session": session or os.environ.get("NVWA_SESSION") or "nvwa",
        "log": str(home.log),
    }
    _write_new_state(store, state)
    if kind == "tmux":
        return _spawn_tmux(store, state)
    return _spawn_subprocess(store, state)


def _spawn_subprocess(store: Path, state: dict) -> dict:
    name = state["name"]
    append_log(AgentPaths(store, name).log, f"[nvwa] launching {state['backend']}\n")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--supervise",
        name,
        "--store",
        str(Path(store).resolve()),
    ]
    kwargs = dict(
        cwd=str(Path(store).resolve().parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    # Hold the Popen until the supervisor exits so it is reaped (no zombie,
    # no ResourceWarning when the handle is discarded).
    threading.Thread(target=_reap, args=(proc,), name="nvwa-reap", daemon=True).start()
    # Do not write status here. The supervisor may already have set "running",
    # and a second write would clobber it back to "starting".
    return update_state(store, name, supervisor_pid=proc.pid)


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    except Exception:
        return


def say(store: Path, name: str, text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise NvwaError("say text is empty")
    state = _refresh(store, load_state(store, name))
    home = AgentPaths(store, name)
    if state.get("status") not in ("running", "starting"):
        raise NvwaError(f"agent {name!r} is not running")
    if state.get("runner") == "tmux":
        if state.get("status") != "running":
            raise NvwaError(f"agent {name!r} is not running")
        _tmux_send(state.get("session") or "nvwa", name, text)
        append_log(home.log, f">>> {text.rstrip()}\n")
        return
    home.inbox.mkdir(parents=True, exist_ok=True)
    filename = f"{time.time_ns()}-{os.getpid()}.txt"
    _atomic_write(home.inbox / filename, text)


def read_log_tail(store: Path, name: str, lines: int = 40) -> str:
    if lines < 1:
        raise NvwaError("lines must be >= 1")
    home = AgentPaths(store, name)
    load_state(store, name)
    if not home.log.exists():
        return ""
    text = home.log.read_text(encoding="utf-8", errors="replace")
    chunk = text.splitlines()[-lines:]
    return "\n".join(chunk)


def follow_log(store: Path, name: str, lines: int = 40, follow: bool = False, out=None) -> None:
    """Print the log tail. With ``follow``, keep printing new bytes until Ctrl-C."""
    out = out or sys.stdout
    home = AgentPaths(store, name)
    load_state(store, name)
    if home.log.exists():
        data = home.log.read_bytes()
    else:
        data = b""
    shown = "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])
    if shown:
        out.write(shown + "\n")
        out.flush()
    if not follow:
        return
    offset = len(data)
    try:
        while True:
            time.sleep(0.2)
            if not home.log.exists():
                continue
            size = home.log.stat().st_size
            if size < offset:
                offset = 0
            if size == offset:
                continue
            with home.log.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
            offset += len(chunk)
            out.write(chunk.decode("utf-8", errors="replace"))
            out.flush()
    except KeyboardInterrupt:
        return


def stop_agent(store: Path, name: str) -> dict:
    home = AgentPaths(store, name)
    state = load_state(store, name)
    home.dir.mkdir(parents=True, exist_ok=True)
    home.stop.write_text("stop\n", encoding="utf-8")
    if state.get("runner") == "tmux":
        _tmux_kill(state.get("session") or "nvwa", name)
        return update_state(store, name, status="stopped")
    deadline = time.time() + 3
    while time.time() < deadline:
        current = load_state(store, name)
        sup = current.get("supervisor_pid")
        if current.get("status") in ("stopped", "exited") and not pid_alive(sup):
            break
        if not pid_alive(sup) and not pid_alive(current.get("pid")):
            break
        time.sleep(0.05)
    current = load_state(store, name)
    for pid in (current.get("pid"), current.get("supervisor_pid")):
        if pid_alive(pid):
            terminate_pid(pid)
    return update_state(store, name, status="stopped")


def stop_all(store: Path) -> None:
    for agent in list(list_agents(store)):
        try:
            stop_agent(store, agent["name"])
        except NvwaError:
            continue


# --------------------------------------------------------------------------- #
# subprocess supervisor
# --------------------------------------------------------------------------- #
def supervise(store: Path, name: str) -> int:
    store = Path(store)
    home = AgentPaths(store, name)
    state = load_state(store, name)
    update_state(store, name, supervisor_pid=os.getpid(), status="running")
    if home.stop.exists():
        update_state(store, name, status="stopped", supervisor_pid=os.getpid())
        return 0
    env = env_for(str(state.get("backend") or ""), list(state.get("argv") or []))
    cwd = store.resolve().parent
    try:
        proc = subprocess.Popen(
            list(state["argv"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        append_log(home.log, f"[nvwa] failed to start {state.get('argv')}: {exc}\n")
        update_state(store, name, status="exited", exit_code=127, supervisor_pid=os.getpid())
        return 127
    update_state(store, name, pid=proc.pid, status="running", supervisor_pid=os.getpid())
    done = threading.Event()

    def reader() -> None:
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                append_log(home.log, chunk.decode("utf-8", errors="replace"))
        finally:
            done.set()

    threading.Thread(target=reader, name=f"nvwa-log-{name}", daemon=True).start()
    mission = state.get("mission") or ""
    if isinstance(mission, str) and mission.strip():
        _write_stdin(proc, home, mission)
    exit_code = None
    try:
        while proc.poll() is None:
            if home.stop.exists():
                terminate_pid(proc.pid)
                break
            for message in _drain_inbox(home):
                _write_stdin(proc, home, message)
            done.wait(0.2)
            if done.is_set():
                done.clear()
        try:
            exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_pid(proc.pid)
            exit_code = proc.wait(timeout=2)
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        status = "stopped" if home.stop.exists() else "exited"
        try:
            update_state(
                store, name,
                status=status,
                exit_code=exit_code if exit_code is not None else proc.returncode,
                pid=proc.pid,
                supervisor_pid=os.getpid(),
            )
        except NvwaError:
            pass
        append_log(home.log, f"\n[nvwa] agent {status} code={exit_code}\n")
    return 0


def _write_stdin(proc: subprocess.Popen, home: AgentPaths, text: str) -> None:
    line = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    append_log(home.log, f">>> {line}\n")
    if proc.stdin is None or proc.poll() is not None:
        return
    try:
        proc.stdin.write((line + "\n").encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        append_log(home.log, f"[nvwa] stdin write failed: {exc}\n")


def _drain_inbox(home: AgentPaths) -> list[str]:
    if not home.inbox.exists():
        return []
    messages = []
    for path in sorted(p for p in home.inbox.iterdir() if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        messages.append(text)
    return messages


# --------------------------------------------------------------------------- #
# optional tmux runner
# --------------------------------------------------------------------------- #
def _tmux(args: list[str], timeout: int = 15, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except FileNotFoundError as exc:
        raise NvwaError("tmux not found on PATH; install tmux or use NVWA_RUNNER=subprocess") from exc
    except subprocess.TimeoutExpired as exc:
        raise NvwaError("tmux command timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise NvwaError(detail or f"tmux exited {exc.returncode}") from exc


def _tmux_window_exists(session: str, name: str) -> bool:
    proc = _tmux(["list-windows", "-t", session, "-F", "#W"], check=False)
    if proc.returncode != 0:
        return False
    return name in {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _tmux_export_keys(session: str, argv0: str) -> None:
    base = Path(argv0).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base == "codex":
        key = (
            os.environ.get("CODEX_API_KEY")
            or os.environ.get("FG_CODEX_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        if key:
            _tmux(["set-environment", "-t", session, "CODEX_API_KEY", key])
    elif base == "grok":
        key = os.environ.get("XAI_API_KEY") or os.environ.get("FG_GROK_API_KEY") or ""
        if key:
            _tmux(["set-environment", "-t", session, "XAI_API_KEY", key])


def _spawn_tmux(store: Path, state: dict) -> dict:
    name = state["name"]
    session = state.get("session") or "nvwa"
    if _tmux(["has-session", "-t", session], check=False).returncode != 0:
        _tmux(["new-session", "-d", "-s", session])
    windows = _tmux(["list-windows", "-t", session, "-F", "#W"], check=False)
    names = [line.strip() for line in windows.stdout.splitlines() if line.strip()]
    if len([n for n in names if n != "nvwa"]) >= max_workers():
        raise NvwaError(f"CAP: {max_workers()} agents — stop one first")
    if name in names:
        _tmux(["kill-window", "-t", f"{session}:{name}"], check=False)
    _tmux_export_keys(session, state["argv"][0])
    quoted = " ".join(shlex.quote(part) for part in state["argv"])
    shell = f"{quoted} 2>&1; echo NVWA_PANE_EXITED; exec sleep infinity"
    _tmux(["new-window", "-d", "-t", session, "-n", name, shell])
    append_log(AgentPaths(store, name).log, f"[nvwa] tmux window {session}:{name}\n")
    ready = _tmux_wait_ready(session, name)
    mission = state.get("mission") or ""
    if ready and isinstance(mission, str) and mission.strip():
        _tmux_send(session, name, mission)
        append_log(AgentPaths(store, name).log, f">>> {mission.rstrip()}\n")
    elif not ready:
        append_log(
            AgentPaths(store, name).log,
            "[nvwa] input box never appeared; pane left for inspection\n",
        )
    return update_state(store, name, status="running", session=session, pid=None)


def _tmux_capture(session: str, name: str) -> str:
    proc = _tmux(["capture-pane", "-p", "-t", f"{session}:{name}"], check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _tmux_wait_ready(session: str, name: str) -> bool:
    pattern = os.environ.get("NVWA_READY_PATTERN") or r"❯|›|codex>|grok>"
    try:
        limit = int(os.environ.get("NVWA_READY_TIMEOUT", "60"))
    except ValueError:
        limit = 60
    waited = 0
    while waited < limit:
        pane = _tmux_capture(session, name)
        if re.search(pattern, pane):
            return True
        time.sleep(0.5)
        waited += 0.5
    return False


def _tmux_send(session: str, name: str, text: str) -> None:
    target = f"{session}:{name}"
    _tmux(["send-keys", "-t", target, "-l", "--", text.rstrip("\n")])
    time.sleep(0.2)
    _tmux(["send-keys", "-t", target, "Enter"])


def _tmux_kill(session: str, name: str) -> None:
    _tmux(["kill-window", "-t", f"{session}:{name}"], check=False)


def tmux_listen(store: Path, name: str, lines: int) -> str:
    state = load_state(store, name)
    if state.get("runner") != "tmux":
        return read_log_tail(store, name, lines)
    text = _tmux_capture(state.get("session") or "nvwa", name)
    return "\n".join(text.splitlines()[-lines:])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "--supervise":
        sys.stderr.write("runner.py is the nvwa supervisor; use the nvwa command\n")
        return 2
    if len(argv) < 4 or argv[2] != "--store":
        sys.stderr.write("usage: runner.py --supervise NAME --store DIR\n")
        return 2
    name, store = argv[1], Path(argv[3])
    try:
        return supervise(store, name)
    except Exception as exc:
        try:
            append_log(AgentPaths(store, name).log, f"[nvwa] supervisor error: {exc}\n")
            update_state(store, name, status="exited", exit_code=1)
        except Exception:
            sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
