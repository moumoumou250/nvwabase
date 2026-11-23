"""Backend selection for fgc dispatch: Claude, Codex CLI, Grok CLI."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "lib"))

import backends  # noqa: E402
import fg  # noqa: E402

SECRET = "sk-test-secret-do-not-print"

CLEAN_ENV = {
    "FG_BACKEND": "",
    "NVWA_BACKEND": "",
    "FG_MODEL": "",
    "FG_CLAUDE_MODEL": "",
    "FG_CODEX_MODEL": "",
    "FG_GROK_MODEL": "",
    "FG_CLAUDE_BIN": "",
    "FG_CODEX_BIN": "",
    "FG_GROK_BIN": "",
    "CODEX_API_KEY": "",
    "OPENAI_API_KEY": "",
    "FG_CODEX_API_KEY": "",
    "XAI_API_KEY": "",
    "FG_GROK_API_KEY": "",
}


class ResolveTests(unittest.TestCase):
    def test_default_backend_is_claude(self):
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=False):
            self.assertEqual(backends.resolve_backend(None), "claude")
            self.assertEqual(backends.resolve_model("claude", None), "sonnet")
            self.assertEqual(backends.resolve_binary("claude"), "claude")

    def test_flag_beats_env_and_aliases(self):
        with mock.patch.dict(os.environ, {**CLEAN_ENV, "FG_BACKEND": "claude"}, clear=False):
            self.assertEqual(backends.resolve_backend("openai"), "codex")
            self.assertEqual(backends.resolve_backend("grok-cli"), "grok")
            self.assertEqual(backends.resolve_backend("XAI"), "grok")

    def test_env_order_and_unknown_name(self):
        with mock.patch.dict(os.environ, {**CLEAN_ENV, "NVWA_BACKEND": "codex"}, clear=False):
            self.assertEqual(backends.resolve_backend(None), "codex")
        with mock.patch.dict(
            os.environ, {**CLEAN_ENV, "FG_BACKEND": "grok", "NVWA_BACKEND": "codex"}, clear=False
        ):
            self.assertEqual(backends.resolve_backend(None), "grok")
        with self.assertRaises(backends.BackendError):
            backends.resolve_backend("pentest")

    def test_model_precedence(self):
        env = {
            **CLEAN_ENV,
            "FG_MODEL": "from-generic",
            "FG_CODEX_MODEL": "from-specific",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(backends.resolve_model("codex", None), "from-specific")
            self.assertEqual(backends.resolve_model("codex", "from-flag"), "from-flag")
            self.assertEqual(backends.resolve_model("grok", None), "from-generic")
            self.assertEqual(backends.resolve_model("grok", ""), "")

    def test_codex_and_grok_omit_model_by_default(self):
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=False):
            self.assertEqual(backends.resolve_model("codex", None), "")
            self.assertEqual(backends.resolve_model("grok", None), "")


class ArgvTests(unittest.TestCase):
    def test_codex_argv_keeps_prompt_and_system_off_the_command_line(self):
        argv = backends.build_argv(
            "codex",
            binary="/opt/bin/codex",
            model="gpt-6.1-sol",
            skip_permissions=True,
            append_system="SYSTEM " + SECRET,
            cwd=Path("/work"),
        )
        self.assertEqual(argv[0], "/opt/bin/codex")
        self.assertEqual(argv[1:3], ["exec", "--json"])
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[-1], "-")
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertNotIn("--append-system-prompt", argv)
        body = backends.compose_prompt("do the task", "SYSTEM " + SECRET, inline_system=True)
        self.assertTrue(body.startswith("SYSTEM " + SECRET))
        self.assertIn("do the task", body)

    def test_codex_without_model_or_bypass(self):
        argv = backends.build_argv(
            "codex", binary="codex", model="", skip_permissions=False,
            append_system=None, cwd=Path("/work"),
        )
        self.assertNotIn("--model", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_grok_argv(self):
        argv = backends.build_argv(
            "grok",
            binary="/opt/bin/grok",
            model="",
            skip_permissions=True,
            append_system="be careful",
            cwd=Path("/work"),
            prompt_file="/tmp/prompt.txt",
        )
        self.assertEqual(argv[0], "/opt/bin/grok")
        self.assertIn("--prompt-file", argv)
        self.assertIn("/tmp/prompt.txt", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--always-approve", argv)
        self.assertIn("--append-system-prompt", argv)
        self.assertIn("be careful", argv)
        self.assertNotIn("--model", argv)
        self.assertIn("--no-auto-update", argv)

    def test_claude_argv_matches_historical_flags(self):
        argv = backends.build_argv(
            "claude", binary="claude", model="sonnet", skip_permissions=True,
            append_system="extra", cwd=Path("/work"),
        )
        self.assertEqual(
            argv,
            ["claude", "-p", "--output-format", "json", "--model", "sonnet",
             "--dangerously-skip-permissions", "--append-system-prompt", "extra"],
        )


class KeyEnvTests(unittest.TestCase):
    def test_openai_key_is_copied_and_does_not_override_codex_key(self):
        env = backends.child_env("codex", {"OPENAI_API_KEY": SECRET, "PATH": "/bin"})
        self.assertEqual(env["CODEX_API_KEY"], SECRET)
        env = backends.child_env("codex", {
            "CODEX_API_KEY": "already",
            "OPENAI_API_KEY": SECRET,
            "FG_CODEX_API_KEY": "other",
        })
        self.assertEqual(env["CODEX_API_KEY"], "already")
        env = backends.child_env("codex", {
            "FG_CODEX_API_KEY": "from-fg",
            "OPENAI_API_KEY": SECRET,
        })
        self.assertEqual(env["CODEX_API_KEY"], "from-fg")

    def test_grok_key_copy_does_not_override(self):
        env = backends.child_env("grok", {"FG_GROK_API_KEY": SECRET})
        self.assertEqual(env["XAI_API_KEY"], SECRET)
        env = backends.child_env("grok", {"XAI_API_KEY": "kept", "FG_GROK_API_KEY": SECRET})
        self.assertEqual(env["XAI_API_KEY"], "kept")

    def test_status_never_contains_the_key(self):
        with mock.patch.dict(os.environ, {**CLEAN_ENV, "OPENAI_API_KEY": SECRET}, clear=False):
            status = backends.api_key_status("codex")
        self.assertIn("OPENAI_API_KEY", status)
        self.assertNotIn(SECRET, status)
        with mock.patch.dict(os.environ, {**CLEAN_ENV, "XAI_API_KEY": SECRET}, clear=False):
            status = backends.api_key_status("grok")
        self.assertNotIn(SECRET, status)


class ParseTests(unittest.TestCase):
    def test_codex_jsonl_uses_the_last_agent_message(self):
        stdout = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"draft"}}',
            '{"type":"item.completed","item":{"id":"2","type":"agent_message","text":"{\\"accepted\\": true}"}}',
            '{"type":"turn.completed","usage":{}}',
        ])
        self.assertEqual(backends.parse_codex_stdout(stdout), '{"accepted": true}')

    def test_codex_failure_event(self):
        stdout = '{"type":"turn.failed","error":"boom"}'
        with self.assertRaisesRegex(backends.BackendError, "boom"):
            backends.parse_codex_stdout(stdout)

    def test_grok_json_text_and_error(self):
        self.assertEqual(
            backends.parse_grok_stdout('{"text":"hello","stopReason":"end_turn"}'),
            "hello",
        )
        with self.assertRaisesRegex(backends.BackendError, "no session"):
            backends.parse_grok_stdout('log line\n{"type":"error","message":"no session"}')


class RunAgentTests(unittest.TestCase):
    def test_codex_invocation_passes_key_only_in_env(self):
        captured = {}

        def fake_run(argv, input=None, **kwargs):
            captured["argv"] = argv
            captured["input"] = input
            captured["env"] = kwargs["env"]
            stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}'
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        with mock.patch.dict(os.environ, {**CLEAN_ENV, "OPENAI_API_KEY": SECRET, "FG_CODEX_BIN": "codex"}, clear=False):
            with mock.patch("backends.subprocess.run", side_effect=fake_run):
                envelope = backends.run_agent(
                    "the task",
                    backend="codex",
                    model="gpt-test",
                    timeout=5,
                    skip_permissions=False,
                    append_system="sys " + SECRET,
                    cwd=Path("/work"),
                )
        self.assertEqual(envelope["result"], "ok")
        self.assertEqual(envelope["backend"], "codex")
        self.assertNotIn(SECRET, " ".join(captured["argv"]))
        self.assertEqual(captured["env"]["CODEX_API_KEY"], SECRET)
        self.assertIn(SECRET, captured["input"])
        self.assertIn("the task", captured["input"])
        self.assertIn("--model", captured["argv"])
        self.assertIn("gpt-test", captured["argv"])

    def test_grok_prompt_file_is_removed(self):
        captured = {}

        def fake_run(argv, input=None, **kwargs):
            path = Path(argv[argv.index("--prompt-file") + 1])
            captured["text"] = path.read_text(encoding="utf-8")
            captured["mode"] = path.stat().st_mode & 0o777
            captured["path"] = path
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, stdout='{"text":"done"}', stderr="")

        with mock.patch.dict(os.environ, {**CLEAN_ENV, "FG_GROK_API_KEY": SECRET}, clear=False):
            with mock.patch("backends.subprocess.run", side_effect=fake_run):
                envelope = backends.run_agent(
                    "grok task",
                    backend="grok",
                    model="",
                    timeout=5,
                    skip_permissions=True,
                    append_system=None,
                    cwd=Path("/work"),
                )
        self.assertEqual(envelope["result"], "done")
        self.assertEqual(captured["text"], "grok task")
        if os.name == "nt":
            # Windows reports st_mode as 0o666 even after chmod; the file is
            # still removed, which is the cross-process leak we care about.
            self.assertIn(captured["mode"], (0o600, 0o666))
        else:
            self.assertEqual(captured["mode"], 0o600)
        self.assertFalse(captured["path"].exists())
        self.assertEqual(captured["env"]["XAI_API_KEY"], SECRET)
        self.assertNotIn(SECRET, captured["text"])

    def test_missing_binary_error_does_not_echo_the_key(self):
        with mock.patch.dict(
            os.environ,
            {**CLEAN_ENV, "FG_CODEX_BIN": "/no/such/codex-binary", "OPENAI_API_KEY": SECRET},
            clear=False,
        ):
            with self.assertRaises(backends.BackendError) as caught:
                backends.run_agent(
                    "hi", backend="codex", model="", timeout=5,
                    skip_permissions=False, append_system=None, cwd=Path("."),
                )
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn("not found", str(caught.exception))


class DispatchSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = fg.Store(Path(self.tmp.name) / ".fg")
        self.store.init("test", "ship it")

    def tearDown(self):
        self.tmp.cleanup()

    def test_backend_command_hides_the_key(self):
        env = {**CLEAN_ENV, "FG_BACKEND": "codex", "FG_CODEX_MODEL": "gpt-test", "OPENAI_API_KEY": SECRET}
        with mock.patch.dict(os.environ, env, clear=False):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = fg.main(["backend"])
        text = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("backend: codex", text)
        self.assertIn("model: gpt-test", text)
        self.assertIn("OPENAI_API_KEY", text)
        self.assertNotIn(SECRET, text)

    def test_dry_run_does_not_call_a_backend(self):
        with mock.patch("backends.run_agent") as run, mock.patch.object(fg, "run_claude") as claude:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fg.main([
                    "--store", str(self.store.root),
                    "dispatch", "reason", "--dry-run", "--backend", "grok",
                ])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        claude.assert_not_called()

    def test_dispatch_reason_codex_calls_run_agent(self):
        payload = '{"accepted": true, "data": {}}'
        with mock.patch(
            "backends.run_agent",
            return_value={"result": payload, "is_error": False, "backend": "codex"},
        ) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fg.main([
                    "--store", str(self.store.root),
                    "dispatch", "reason", "--backend", "codex", "--model", "gpt-test",
                ])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.kwargs["backend"], "codex")
        self.assertEqual(run.call_args.kwargs["model"], "gpt-test")

    def test_dispatch_reason_claude_still_uses_run_claude(self):
        payload = '{"accepted": true, "data": {}}'
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=False), mock.patch(
            "backends.run_agent"
        ) as run, mock.patch.object(
            fg, "run_claude", return_value={"result": payload}
        ) as claude:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fg.main([
                    "--store", str(self.store.root),
                    "dispatch", "reason", "--backend", "claude",
                ])
        self.assertEqual(rc, 0)
        claude.assert_called_once()
        run.assert_not_called()
        self.assertEqual(claude.call_args.kwargs["model"], "sonnet")


if __name__ == "__main__":
    unittest.main()
