"""Settings helper (scripts/pp_agent.py) + web handoff: whitelist, validation, rollback, request files."""
import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_agent(project: Path):
    os.environ["POOR_PUPPY_DIR"] = str(project)
    spec = importlib.util.spec_from_file_location("pp_agent_t", ROOT / "scripts" / "pp_agent.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PROJECT, m.AGENT = project, project / "data" / "agent"
    m.REQ, m.RES = m.AGENT / "requests", m.AGENT / "results"
    m.HTPASSWD, m.NGINX_TOKEN = project / "htpasswd", project / "token.conf"
    m.pps.ENV_FILE, m.pps.EXAMPLE = project / ".env", project / ".env.example"
    m.chown_for_container = lambda p: None
    return m


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name)
        (self.p / ".env").write_text("ALERT_MODE=every_miss\nHA_WEBHOOK_URL=https://ha.example/api/webhook/abcd1234\nMOLLY_PASS=secret\nTZ=America/New_York\n")
        (self.p / ".env.example").write_text("")
        self.a = load_agent(self.p)
        self.calls = []
        self.a.run_update_pp = lambda *args, **k: (self.calls.append(args) or (True, "ok"))

    def tearDown(self):
        self.td.cleanup()

    def env(self):
        return self.a.pps.read_env(self.p / ".env")

    def test_validation(self):
        v = self.a.validate
        self.assertEqual(v("TTS_VOLUME", "3.5"), "3.5")
        self.assertEqual(v("KEEP_FOOTAGE", "TRUE"), "true")
        self.assertEqual(v("DAILY_SUMMARY_TIME", ""), "")
        for key, bad in (("TTS_VOLUME", "99"), ("ALERT_MODE", "sometimes"), ("DAILY_SUMMARY_TIME", "25:00"),
                         ("HA_WEBHOOK_URL", "javascript:alert(1)"), ("TZ", ""), ("TZ", "x; rm -rf /"),
                         ("CAPTURE_RETENTION_DAYS", "1\nMOLLY_PASS=pwned")):
            with self.assertRaises(ValueError, msg=(key, bad)):
                v(key, bad)

    def test_apply_settings_whitelist_secret_keep_and_restart(self):
        with self.assertRaises(ValueError):
            self.a.do_apply_settings({"values": {"MOLLY_PASS": "x"}})        # never editable from the web
        ok, out = self.a.do_apply_settings({"values": {"TTS_VOLUME": "4", "HA_WEBHOOK_URL": ""}})
        self.assertTrue(ok, out)
        e = self.env()
        self.assertEqual(e["TTS_VOLUME"], "4")
        self.assertEqual(e["HA_WEBHOOK_URL"], "https://ha.example/api/webhook/abcd1234")  # empty secret = keep
        self.assertEqual(e["MOLLY_PASS"], "secret")
        self.assertEqual(self.calls, [("--no-pull",)])
        self.a.do_apply_settings({"clear": ["HA_WEBHOOK_URL"]})
        self.assertEqual(self.env()["HA_WEBHOOK_URL"], "")

    def test_apply_settings_restores_on_failure(self):
        self.a.run_update_pp = lambda *args, **k: (False, "meal guard: next meal in 3 min")
        ok, out = self.a.do_apply_settings({"values": {"TTS_VOLUME": "5"}})
        self.assertFalse(ok)
        self.assertIn("restored", out)
        self.assertNotIn("TTS_VOLUME", self.env())

    def test_set_password_validates_hash(self):
        with self.assertRaises(ValueError):
            self.a.do_set_password({"username": "puppy", "hash": "plaintext"})
        with self.assertRaises(ValueError):
            self.a.do_set_password({"username": "bad user", "hash": "$6$abc$" + "a" * 86})
        ok, _ = self.a.do_set_password({"username": "puppy", "hash": "$6$abcdefgh$" + "A" * 86})
        self.assertTrue(ok)
        self.assertTrue((self.p / "htpasswd").read_text().startswith("puppy:$6$abcdefgh$"))

    def test_process_runs_only_known_actions_and_masks_secrets(self):
        self.a.REQ.mkdir(parents=True)
        (self.a.REQ / "20260101-aaaa1111.json").write_text(json.dumps({"action": "status"}))
        (self.a.REQ / "20260101-bbbb2222.json").write_text(json.dumps({"action": "rm -rf /"}))
        self.a.process()
        good = json.loads((self.a.RES / "20260101-aaaa1111.json").read_text())
        bad = json.loads((self.a.RES / "20260101-bbbb2222.json").read_text())
        self.assertTrue(good["ok"])
        self.assertFalse(bad["ok"])
        self.assertIn("unknown action", bad["output"])
        self.assertEqual(list(self.a.REQ.glob("*.json")), [])
        snap = json.loads((self.a.AGENT / "settings.json").read_text())
        self.assertEqual(snap["values"]["HA_WEBHOOK_URL"], {"set": True, "masked": "••••1234"})
        self.assertNotIn("MOLLY_PASS", json.dumps(snap))


class WebHandoffTests(unittest.TestCase):
    def setUp(self):
        from tests.test_updater import _fake_watchdog
        from watchdog.db import KibbleDatabase

        self.td = tempfile.TemporaryDirectory()
        self.w = _fake_watchdog(self.td.name)
        self.w.db = KibbleDatabase(str(Path(self.td.name) / "data" / "k.db")) if (Path(self.td.name) / "data").mkdir() is None else None
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.w.cfg.webhook_listen_host, self.w.cfg.webhook_listen_port, self.w.cfg.webhook_secret = "127.0.0.1", self.port, "tok"
        self.server = self.w.start_webhook_server()
        self.agent = Path(self.td.name) / "data" / "agent"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.td.cleanup()

    def req(self, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"X-Webhook-Token": "tok"},
                                   data=None if body is None else json.dumps(body).encode())
        try:
            resp = urllib.request.urlopen(r, timeout=10)
            return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_not_installed_then_queue_and_result(self):
        self.assertEqual(self.req("/api/settings")[0], 503)
        self.assertEqual(self.req("/api/agent", {"action": "status"})[0], 503)
        (self.agent / "requests").mkdir(parents=True)
        (self.agent / "results").mkdir()
        (self.agent / "settings.json").write_text(json.dumps({"schema": [], "values": {}}))
        self.assertTrue(self.req("/api/settings")[1]["installed"])
        self.assertEqual(self.req("/api/agent", {"action": "format_disk"})[0], 400)
        code, r = self.req("/api/agent", {"action": "status"})
        self.assertEqual(code, 202)
        queued = json.loads((self.agent / "requests" / f"{r['id']}.json").read_text())
        self.assertEqual(queued["action"], "status")
        self.assertEqual(self.req(f"/api/agent/{r['id']}")[1]["status"], "pending")
        (self.agent / "results" / f"{r['id']}.json").write_text(json.dumps({"status": "done", "ok": True, "output": "x"}))
        self.assertTrue(self.req(f"/api/agent/{r['id']}")[1]["ok"])
        self.assertEqual(self.req("/api/agent/../../etc/passwd")[0], 400)

    def test_password_is_hashed_before_it_is_written(self):
        (self.agent / "requests").mkdir(parents=True)
        (self.agent / "settings.json").write_text("{}")
        self.assertEqual(self.req("/api/agent", {"action": "set_password", "params": {"username": "puppy", "password": "short"}})[0], 400)
        if sys.platform != "linux":
            self.skipTest("SHA-512 crypt needs glibc (the Docker image); macOS crypt can't make $6$ hashes")
        code, r = self.req("/api/agent", {"action": "set_password", "params": {"username": "puppy", "password": "correct horse battery"}})
        self.assertEqual(code, 202)
        raw = (self.agent / "requests" / f"{r['id']}.json").read_text()
        self.assertNotIn("correct horse", raw)
        self.assertRegex(json.loads(raw)["params"]["hash"], r"^\$6\$")


if __name__ == "__main__":
    unittest.main()
