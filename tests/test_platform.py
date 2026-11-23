"""Lock, subprocess runner, and local panel API."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import fg  # noqa: E402
import locking  # noqa: E402
import runner  # noqa: E402
import ui  # noqa: E402

ECHO = """\
import sys
print("READY", flush=True)
for line in sys.stdin:
    sys.stdout.write("ECHO " + line)
    sys.stdout.flush()
"""


def _wait_for(predicate, timeout=8.0, detail=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(detail or "timed out")


class LockTests(unittest.TestCase):
    def test_fg_does_not_import_fcntl(self):
        text = (ROOT / "lib" / "fg.py").read_text(encoding="utf-8")
        self.assertNotIn("import fcntl", text)
        locking_text = (ROOT / "lib" / "locking.py").read_text(encoding="utf-8")
        self.assertIn("import fcntl", locking_text)
        self.assertIn("import msvcrt", locking_text)

    def test_lock_excludes_other_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            state = {"held": 0, "overlap": False}

            def worker():
                with locking.file_lock(path):
                    state["held"] += 1
                    if state["held"] != 1:
                        state["overlap"] = True
                    time.sleep(0.08)
                    state["held"] -= 1

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(state["overlap"])
            self.assertEqual(state["held"], 0)
            self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_store_lock_and_init_allow_agents_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".fg"
            (root / "agents").mkdir(parents=True)
            (root / "agents" / "note.txt").write_text("keep", encoding="utf-8")
            store = fg.Store(root)
            store.init("origin", "a goal")
            self.assertTrue(store.exists())
            self.assertTrue((root / "agents" / "note.txt").exists())
            seen = {"held": 0, "overlap": False}

            def worker():
                with store.locked():
                    seen["held"] += 1
                    if seen["held"] != 1:
                        seen["overlap"] = True
                    time.sleep(0.05)
                    seen["held"] -= 1

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(seen["overlap"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / ".fg"
        self.store.mkdir()
        self.script = Path(self.tmp.name) / "echo_agent.py"
        self.script.write_text(ECHO, encoding="utf-8")
        self._env = os.environ.get("NVWA_RUNNER")
        self._agent = os.environ.pop("NVWA_AGENT", None)
        os.environ["NVWA_RUNNER"] = "subprocess"

    def tearDown(self):
        runner.stop_all(self.store)
        if self._env is None:
            os.environ.pop("NVWA_RUNNER", None)
        else:
            os.environ["NVWA_RUNNER"] = self._env
        if self._agent is not None:
            os.environ["NVWA_AGENT"] = self._agent
        self.tmp.cleanup()

    def _argv(self):
        return [sys.executable, str(self.script)]

    def test_spawn_say_listen_stop(self):
        state = runner.spawn_agent(
            self.store, name="w1", argv=self._argv(), mission="",
        )
        self.assertEqual(state["name"], "w1")
        self.assertEqual(state["runner"], "subprocess")
        log = self.store / "agents" / "w1.log"
        _wait_for(lambda: log.exists() and "READY" in log.read_text(encoding="utf-8"), detail="READY missing")
        runner.say(self.store, "w1", "hello")
        _wait_for(
            lambda: "ECHO hello" in log.read_text(encoding="utf-8"),
            detail=log.read_text(encoding="utf-8") if log.exists() else "no log",
        )
        tail = runner.read_log_tail(self.store, "w1", 20)
        self.assertIn(">>> hello", tail)
        listed = runner.list_agents(self.store)
        self.assertEqual(listed[0]["status"], "running")
        stopped = runner.stop_agent(self.store, "w1")
        self.assertEqual(stopped["status"], "stopped")
        self.assertIn("w1", runner.format_ps(runner.list_agents(self.store)))

    def _nvwa(self, *args):
        env = os.environ.copy()
        env["NVWA_RUNNER"] = "subprocess"
        env.pop("NVWA_AGENT", None)
        return subprocess.run(
            [sys.executable, str(ROOT / "nvwa"), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmp.name,
            timeout=20,
        )

    def test_cli_listen_and_unknown_name(self):
        quoted = " ".join(f'"{part}"' for part in (sys.executable, str(self.script)))
        spawned = self._nvwa("--store", str(self.store), "spawn", "--name", "cli1", "--agent", quoted)
        self.assertEqual(spawned.returncode, 0, spawned.stderr)
        self.assertIn("SPAWNED cli1", spawned.stdout)
        log = self.store / "agents" / "cli1.log"
        _wait_for(
            lambda: log.exists() and "READY" in log.read_text(encoding="utf-8"),
            detail=log.read_text(encoding="utf-8") if log.exists() else spawned.stderr,
        )
        said = self._nvwa("--store", str(self.store), "say", "cli1", "ping")
        self.assertEqual(said.returncode, 0, said.stderr)
        _wait_for(
            lambda: "ECHO ping" in log.read_text(encoding="utf-8"),
            detail=log.read_text(encoding="utf-8"),
        )
        heard = self._nvwa("--store", str(self.store), "listen", "cli1", "20")
        self.assertEqual(heard.returncode, 0, heard.stderr)
        self.assertIn("ECHO ping", heard.stdout)
        stopped = self._nvwa("--store", str(self.store), "stop", "cli1")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        missing = self._nvwa("--store", str(self.store), "say", "missing", "x")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no such agent", missing.stderr)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / ".fg"
        fg.Store(self.store).init("test", "ship the panel")
        self.script = Path(self.tmp.name) / "echo_agent.py"
        self.script.write_text(ECHO, encoding="utf-8")
        self.panel = ui.Panel(self.store)
        self.base = self.panel.serve_background("127.0.0.1", 0)
        os.environ["NVWA_RUNNER"] = "subprocess"

    def tearDown(self):
        runner.stop_all(self.store)
        self.panel.close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, token=True, origin=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {}
        if token:
            headers["X-Nvwa-Token"] = self.panel.token
        if data is not None:
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed

    def test_token_origin_spawn_say_stream_graph(self):
        status, payload = self._request("GET", "/api/agents", token=False)
        self.assertEqual(status, 401)
        self.assertNotIn(self.panel.token, json.dumps(payload))

        status, payload = self._request(
            "POST", "/api/agents/w1/say", {"text": "nope"}, origin="http://evil.example",
        )
        self.assertEqual(status, 403)

        page = urllib.request.urlopen(self.base + "/", timeout=5).read().decode("utf-8")
        self.assertIn(self.panel.token, page)
        self.assertIn("/graph", page)

        status, payload = self._request("POST", "/api/agents", {
            "name": "w1",
            "argv": [sys.executable, str(self.script)],
            "mission": "",
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["agent"]["name"], "w1")
        self.assertEqual(payload["agent"]["runner"], "subprocess")

        log = self.store / "agents" / "w1.log"
        _wait_for(lambda: log.exists() and "READY" in log.read_text(encoding="utf-8"))

        host, port = self.base.rsplit(":", 1)
        host = host.split("://", 1)[1]
        sock = socket.create_connection((host, int(port)), timeout=5)
        request = (
            f"GET /api/agents/w1/stream?token={self.panel.token} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        sock.settimeout(0.5)
        buffered = b""
        deadline = time.time() + 8
        while time.time() < deadline and b"READY" not in buffered:
            try:
                chunk = sock.recv(4096)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                break
            buffered += chunk
        self.assertIn(b"READY", buffered)

        status, payload = self._request("POST", "/api/agents/w1/say", {"text": "panel"})
        self.assertEqual(status, 200, payload)
        deadline = time.time() + 8
        while time.time() < deadline and b"ECHO panel" not in buffered:
            try:
                chunk = sock.recv(4096)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                break
            buffered += chunk
        sock.close()
        self.assertIn(b"ECHO panel", buffered)

        status, payload = self._request("GET", "/api/graph")
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"]["goal"], "ship the panel")
        self.assertTrue(any(fact["id"] == "goal" for fact in payload["facts"]))

        status, payload = self._request("POST", "/api/agents/w1/stop", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["agent"]["status"], "stopped")

        status, agents = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(agents["agents"][0]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
