"""CLI agent backends for ``fgc dispatch`` / ``fgc auto``.

The fact graph stays backend-agnostic. Each backend is an external command
that accepts a prompt and returns text. ``run_agent`` normalizes that text
into the envelope the rest of fgc already expects::

    {"result": "<model text>", "is_error": false}

Supported backends
------------------
claude
    Existing Claude Code CLI (implemented in ``fg.py`` as ``run_claude`` so
    the historical flag set stays in one place). This module only resolves
    its binary / model / name.

codex
    OpenAI Codex CLI, non-interactive mode. Chosen over a raw HTTPS call
    because dispatch (especially explore / verify) needs a tool-using agent,
    and the project has no third-party dependencies. Invocation::

        codex exec --json --color never --skip-git-repo-check --ephemeral
            --cd <cwd> [--model <id>]
            [--dangerously-bypass-approvals-and-sandbox]
            -

    The prompt is the stdin stream (``-``). ``--json`` prints JSONL events;
    the last ``agent_message`` is the result. Authentication is the Codex
    CLI login, or an API key. The CLI reads ``CODEX_API_KEY`` (it does not
    read ``OPENAI_API_KEY``). When only ``OPENAI_API_KEY`` or
    ``FG_CODEX_API_KEY`` is set, the child process receives it as
    ``CODEX_API_KEY``. Keys are never placed on argv.

grok
    xAI Grok CLI (Grok Build), headless mode. Invocation::

        grok --no-auto-update --output-format json --prompt-file <file>
            --cwd <cwd> [--model <id>] [--always-approve]
            [--append-system-prompt <text>]

    Headless mode does not read a piped prompt, so the prompt is a 0600
    temp file. ``--output-format json`` prints one object whose ``text``
    field is the final message. Authentication is ``grok login`` or
    ``XAI_API_KEY``. ``FG_GROK_API_KEY`` is copied to ``XAI_API_KEY`` for
    the child when ``XAI_API_KEY`` is unset.

Selection
---------
``--backend`` wins, then ``FG_BACKEND``, then ``NVWA_BACKEND``, else
``claude``. Model: ``--model``, then ``FG_<BACKEND>_MODEL``, then
``FG_MODEL``, then the built-in default. Codex and Grok omit ``--model``
when nothing is configured so the CLI's own default is used.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

BACKENDS = ("claude", "codex", "grok")

ALIASES = {
    "claude-code": "claude",
    "openai": "codex",
    "openai-codex": "codex",
    "codex-cli": "codex",
    "grok-cli": "grok",
    "xai": "grok",
    "xai-grok": "grok",
}

# Empty string means "do not pass --model; the CLI uses its configured default".
# Codex and Grok model ids move quickly, so this repo does not pin one.
DEFAULT_MODELS = {
    "claude": "sonnet",
    "codex": "",
    "grok": "",
}

_BINARY_ENV = {
    "claude": "FG_CLAUDE_BIN",
    "codex": "FG_CODEX_BIN",
    "grok": "FG_GROK_BIN",
}
_MODEL_ENV = {
    "claude": "FG_CLAUDE_MODEL",
    "codex": "FG_CODEX_MODEL",
    "grok": "FG_GROK_MODEL",
}
_DEFAULT_BINARY = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
}


class BackendError(Exception):
    """User-facing backend failure. Callers map this to their own error type."""


def resolve_backend(explicit: str | None = None) -> str:
    """Pick a backend name. ``explicit`` is the CLI flag (None if omitted)."""
    if explicit is not None and explicit.strip():
        raw = explicit.strip()
    else:
        raw = os.environ.get("FG_BACKEND") or os.environ.get("NVWA_BACKEND") or "claude"
    name = ALIASES.get(raw.strip().lower(), raw.strip().lower())
    if name not in BACKENDS:
        known = ", ".join(BACKENDS)
        raise BackendError(f"unknown backend {raw!r}; choose one of: {known}")
    return name


def resolve_model(backend: str, explicit: str | None = None) -> str:
    """Resolve the model id for ``backend``.

    An empty return value means the CLI flag should be omitted. ``explicit``
    is the ``--model`` value; ``None`` means the flag was not passed (an
    empty string is an explicit empty model and is returned as-is).
    """
    backend = resolve_backend(backend)
    if explicit is not None:
        return explicit.strip()
    specific = os.environ.get(_MODEL_ENV[backend], "").strip()
    if specific:
        return specific
    generic = os.environ.get("FG_MODEL", "").strip()
    if generic:
        return generic
    return DEFAULT_MODELS[backend]


def resolve_binary(backend: str) -> str:
    backend = resolve_backend(backend)
    configured = os.environ.get(_BINARY_ENV[backend], "").strip()
    return configured or _DEFAULT_BINARY[backend]


def api_key_status(backend: str) -> str:
    """Describe whether a key is configured. Never includes the key itself."""
    backend = resolve_backend(backend)
    if backend == "claude":
        return "n/a (Claude Code login)"
    if backend == "codex":
        if os.environ.get("CODEX_API_KEY", "").strip():
            return "set (CODEX_API_KEY)"
        if os.environ.get("FG_CODEX_API_KEY", "").strip():
            return "set (FG_CODEX_API_KEY → CODEX_API_KEY)"
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return "set (OPENAI_API_KEY → CODEX_API_KEY)"
        return "unset (Codex CLI saved login, if any)"
    if os.environ.get("XAI_API_KEY", "").strip():
        return "set (XAI_API_KEY)"
    if os.environ.get("FG_GROK_API_KEY", "").strip():
        return "set (FG_GROK_API_KEY → XAI_API_KEY)"
    return "unset (grok CLI saved login, if any)"


def child_env(backend: str, base: dict | None = None) -> dict:
    """Environment for the child process. Copies ``base`` (default: os.environ).

    Codex: inject ``CODEX_API_KEY`` only when it is unset, preferring
    ``FG_CODEX_API_KEY`` over ``OPENAI_API_KEY``.
    Grok: inject ``XAI_API_KEY`` from ``FG_GROK_API_KEY`` only when unset.
    An already-exported canonical key is left untouched.
    """
    backend = resolve_backend(backend)
    env = dict(os.environ if base is None else base)
    if backend == "codex" and not env.get("CODEX_API_KEY", "").strip():
        key = env.get("FG_CODEX_API_KEY", "").strip() or env.get("OPENAI_API_KEY", "").strip()
        if key:
            env["CODEX_API_KEY"] = key
    elif backend == "grok" and not env.get("XAI_API_KEY", "").strip():
        key = env.get("FG_GROK_API_KEY", "").strip()
        if key:
            env["XAI_API_KEY"] = key
    return env


def build_argv(
    backend: str,
    *,
    binary: str,
    model: str,
    skip_permissions: bool,
    append_system: str | None,
    cwd: Path,
    prompt_file: str | None = None,
) -> list[str]:
    """Argv for one backend. The prompt itself is never an argument.

    Claude reads the prompt on stdin. Codex reads it on stdin via ``-``.
    Grok reads ``prompt_file`` (required). ``append_system`` is a flag for
    Claude and Grok; Codex has no equivalent flag, so the caller prepends it
    to the stdin prompt.
    """
    backend = resolve_backend(backend)
    if not binary or not binary.strip():
        raise BackendError(f"{backend} binary path cannot be empty")
    cwd_s = str(cwd)
    if backend == "claude":
        argv = [binary, "-p", "--output-format", "json", "--model", model]
        if skip_permissions:
            argv.append("--dangerously-skip-permissions")
        if append_system:
            argv += ["--append-system-prompt", append_system]
        return argv
    if backend == "codex":
        argv = [
            binary, "exec",
            "--json",
            "--color", "never",
            "--skip-git-repo-check",
            "--ephemeral",
            "--cd", cwd_s,
        ]
        if model:
            argv += ["--model", model]
        if skip_permissions:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv.append("-")
        return argv
    if prompt_file is None:
        raise BackendError("grok invocation requires a prompt file")
    argv = [
        binary,
        "--no-auto-update",
        "--output-format", "json",
        "--prompt-file", prompt_file,
        "--cwd", cwd_s,
    ]
    if model:
        argv += ["--model", model]
    if skip_permissions:
        argv.append("--always-approve")
    if append_system:
        argv += ["--append-system-prompt", append_system]
    return argv


def compose_prompt(prompt: str, append_system: str | None, *, inline_system: bool) -> str:
    """Prepend a system addendum when the CLI has no system-prompt flag."""
    if inline_system and append_system:
        return append_system.rstrip() + "\n\n" + prompt
    return prompt


def parse_claude_stdout(stdout: str) -> str:
    """Claude ``--output-format json`` envelope → result text."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(
            f"claude did not return JSON. stdout[:300]={stdout[:300]!r}"
        ) from exc
    if not isinstance(envelope, dict):
        raise BackendError("claude returned a JSON value instead of an object envelope")
    if envelope.get("is_error"):
        result = envelope.get("result", "")
        raise BackendError(f"claude reported error: {str(result)[:300]}")
    result = envelope.get("result", "")
    if not isinstance(result, str):
        raise BackendError("claude response envelope has a non-string result")
    return result


def parse_codex_stdout(stdout: str) -> str:
    """Codex ``exec --json`` JSONL → the last agent message."""
    messages: list[str] = []
    errors: list[str] = []
    saw_json = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        saw_json = True
        kind = event.get("type")
        if kind in ("error", "turn.failed"):
            detail = event.get("message") or event.get("error") or event
            errors.append(_short(detail))
        text = _codex_message_text(event)
        if text:
            messages.append(text)
    if messages:
        return messages[-1]
    if errors:
        raise BackendError(f"codex failed: {errors[-1]}")
    if not saw_json:
        text = stdout.strip()
        if text:
            return text
    raise BackendError(
        f"codex returned no agent message. stdout[:300]={stdout[:300]!r}"
    )


def _codex_message_text(event: dict) -> str | None:
    kind = event.get("type")
    item = event.get("item") if isinstance(event.get("item"), dict) else None
    if kind in ("item.completed", "item.updated") and item is not None:
        if item.get("type") in ("agent_message", "message"):
            return _as_text(item.get("text"))
    if kind in ("agent_message", "message"):
        return _as_text(event.get("text"))
    return None


def parse_grok_stdout(stdout: str) -> str:
    """Grok ``--output-format json`` object → ``text`` (or ``result``)."""
    data = _parse_json_value(stdout)
    if not isinstance(data, dict):
        raise BackendError(
            f"grok did not return a JSON object. stdout[:300]={stdout[:300]!r}"
        )
    if data.get("type") == "error" or data.get("is_error") is True:
        detail = data.get("message") or data.get("error") or data.get("result") or data
        raise BackendError(f"grok failed: {_short(detail)}")
    text = _as_text(data.get("text"))
    if text is None:
        text = _as_text(data.get("result"))
    if text is None:
        raise BackendError(
            f"grok JSON has no text. stdout[:300]={stdout[:300]!r}"
        )
    return text


def _parse_json_value(stdout: str):
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Grok writes one JSON object. Tolerate a short preamble by scanning
    # from the last '{' that parses as an object.
    start = text.rfind("{")
    while start != -1:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            start = text.rfind("{", 0, start)
    return None


def _as_text(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _short(value) -> str:
    if isinstance(value, str):
        return value.strip()[:300]
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[:300]


def run_agent(
    prompt: str,
    *,
    backend: str,
    model: str,
    timeout: int,
    skip_permissions: bool,
    append_system: str | None,
    cwd: Path,
) -> dict:
    """Run Codex or Grok and return ``{"result": str, "is_error": False}``.

    Claude is invoked by ``fg.run_claude`` so its flag set stays unchanged.
    """
    backend = resolve_backend(backend)
    if backend == "claude":
        raise BackendError("claude is invoked through run_claude, not run_agent")
    if timeout < 1:
        raise BackendError(f"{backend} timeout must be at least 1 second")
    binary = resolve_binary(backend)
    inline_system = backend == "codex"
    body = compose_prompt(prompt, append_system, inline_system=inline_system)
    prompt_path: Path | None = None
    try:
        if backend == "grok":
            fd, name = tempfile.mkstemp(prefix="fgc-grok-", suffix=".txt")
            prompt_path = Path(name)
            try:
                os.write(fd, body.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(prompt_path, 0o600)
            argv = build_argv(
                backend, binary=binary, model=model,
                skip_permissions=skip_permissions,
                append_system=append_system, cwd=cwd,
                prompt_file=str(prompt_path),
            )
            stdin = None
        else:
            argv = build_argv(
                backend, binary=binary, model=model,
                skip_permissions=skip_permissions,
                append_system=None, cwd=cwd,
            )
            stdin = body
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd),
                env=child_env(backend),
            )
        except FileNotFoundError as exc:
            raise BackendError(
                f"`{binary}` not found on PATH. Install the {backend} CLI "
                f"or set {_BINARY_ENV[backend]} to its path."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"{backend} dispatch timed out after {timeout}s") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise BackendError(
                f"{backend} exited {proc.returncode}: {detail[:500]}"
            )
        if backend == "codex":
            text = parse_codex_stdout(proc.stdout)
        else:
            text = parse_grok_stdout(proc.stdout)
    finally:
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except OSError:
                pass
    return {"result": text, "is_error": False, "backend": backend}
