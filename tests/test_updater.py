"""Tests for the safe updater (update-pp), the /health meal-guard fields and the /selftest endpoint.

Run:  .venv/bin/python -m unittest tests.test_updater -v

No network, no Docker, no cloud: update-pp runs against a throw-away git remote with a fake `docker`
(PP_DOCKER_BIN) that records every call and returns scripted results; the HTTP endpoints run against a fake
watchdog / feeder client.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WEBHOOK_SECRET", "")

UPDATER = str(ROOT / "update-pp")
LIVE_SCHEDULE = "7f07002010000007f11002010000007f16002010000007f1900171000000000908100000000"
OLD_IMAGE_ID = "sha256:" + "a" * 64
HEALTH_OK = json.dumps({"ok": True, "status": "idle", "busy": False, "meal_count": 2,
                        "next_meal_in_sec": 7200, "last_meal_ago_sec": 7200})
SELFTEST_PASS = json.dumps({"overall": "pass", "duration_sec": 1.0, "checks": [
    {"name": "cloud_api", "status": "pass", "detail": "session valid"},
    {"name": "feeder_online", "status": "pass", "detail": "feeder reports online"},
]}) + "\nHTTP_CODE:200"
SELFTEST_FAIL = json.dumps({"overall": "fail", "duration_sec": 1.0, "checks": [
    {"name": "cloud_api", "status": "fail", "detail": "cloud API unreachable"},
]}) + "\nHTTP_CODE:503"

# Fake docker: logs each call (JSON line) and answers from files in $FAKE_DOCKER_STATE. A file named
# "<key>.<n>" overrides "<key>" after n `compose up` calls (0 = before the switch, 1 = new version, 2 = rollback).
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

st = Path(os.environ["FAKE_DOCKER_STATE"])
args = sys.argv[1:]
calls = st / "calls.log"
prev = [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []
ups = sum(1 for c in prev if c[:2] == ["compose", "up"])
with calls.open("a") as fh:
    fh.write(json.dumps(args) + "\n")

def val(key, default=""):
    for name in (f"{key}.{ups}", key):
        p = st / name
        if p.exists():
            return p.read_text()
    return default

def out(key, default=""):
    sys.stdout.write(val(key, default))
    sys.stdout.write("\n")

if args[0] == "inspect":
    fmt = args[2]
    if "State.Running" in fmt:
        out("running", "true")
    elif "Health.Status" in fmt:
        out("health", "healthy")
    elif "Config.Image" in fmt:
        out("config_image", "poor-puppy-feeder-watchdog")
    elif ".Image" in fmt:
        out("image", "sha256:" + "a" * 64)
    sys.exit(0)
if args[0] == "image":
    if args[1] == "inspect":
        out("created", "2026-11-01T03:00:00.000Z")
    sys.exit(0)
if args[0] in ("tag", "rmi"):
    sys.exit(0)
if args[0] == "compose":
    sub = args[1]
    if sub == "build":
        sys.exit(int(val("build_rc", "0") or 0))
    if sub == "run":
        rc = int(val("smoke_rc", "0") or 0)
        print("SMOKE OK (fake)" if rc == 0 else "SMOKE FAIL (fake)")
        sys.exit(rc)
    if sub == "up":
        sys.exit(int(val("up_rc", "0") or 0))
    if sub == "logs":
        out("logs", "2026-11-06 [INFO] poor_puppy.main: Puppy Feeder Watchdog running (mode=CLOUD). Monitoring meals: ['07:00']")
        sys.exit(0)
    if sub == "exec":
        script = " ".join(args)
        if "/selftest" in script:
            out("selftest", "")
        elif "/health" in script:
            out("health_json", "")
        sys.exit(0)
sys.exit(0)
'''


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(base: Path, v06_updater: str = ""):
    """Remote with v0.5 and v0.6 branches; work clone installed on v0.5."""
    remote, seed, work = base / "remote.git", base / "seed", base / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    for repo in (seed,):
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
    _git(seed, "checkout", "-b", "v0.5")
    (seed / ".env.example").write_text("KEEP_FOOTAGE=true\nUPDATE_GUARD_BEFORE_MIN=10\n")
    (seed / "app.txt").write_text("v0.5\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "v0.5")
    _git(seed, "push", "origin", "v0.5")
    _git(seed, "checkout", "-b", "v0.6")
    (seed / "app.txt").write_text("v0.6\n")
    if v06_updater:
        (seed / "update-pp").write_text(v06_updater)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "v0.6")
    _git(seed, "push", "origin", "v0.6")
    subprocess.run(["git", "clone", "-b", "v0.5", str(remote), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / ".env").write_text("MOLLY_PASS=keepme\nTZ=UTC\n")
    (work / "data").mkdir()
    (work / "captures").mkdir()
    return work, _git(work, "rev-parse", "--short", "HEAD"), _git(work, "rev-parse", "HEAD")


class UpdaterTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.state = self.base / "docker_state"
        self.state.mkdir()
        self.fake = self.base / "fake_docker"
        self.fake.write_text(FAKE_DOCKER)
        self.fake.chmod(0o755)
        self.scripted("health_json", HEALTH_OK)
        self.scripted("selftest", SELFTEST_PASS)

    def tearDown(self):
        self._td.cleanup()

    def scripted(self, key, value):
        (self.state / key).write_text(value)

    def env(self, work, **extra):
        return {
            **os.environ,
            "POOR_PUPPY_DIR": str(work),
            "PP_SKIP_DOCKER": "0",
            "PP_DOCKER_BIN": str(self.fake),
            "FAKE_DOCKER_STATE": str(self.state),
            "PP_HEALTH_RETRIES": "2",
            "PP_HEALTH_SLEEP": "0",
            "PP_LOG_RETRIES": "2",
            "PP_SELFTEST_RETRIES": "2",
            "PP_SELFTEST_SLEEP": "0",
            "PP_GUARD_WAIT_MAX": "0",
            **extra,
        }

    def run_pp(self, work, *args, **extra):
        return subprocess.run([UPDATER, *args], env=self.env(work, **extra), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def calls(self):
        p = self.state / "calls.log"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def compose_calls(self, sub):
        return [c for c in self.calls() if c[:2] == ["compose", sub]]

    def history(self, work):
        p = work / "data" / "update-history.log"
        return p.read_text().splitlines() if p.exists() else []


class TestMealGuard(UpdaterTestBase):
    def test_busy_refuses_before_any_change(self):
        work, old, _ = make_repo(self.base)
        self.scripted("health_json", json.dumps({"ok": True, "busy": True, "meal_count": 1,
                                                 "next_meal_in_sec": 9000, "last_meal_ago_sec": 9000}))
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("verification is running", r.stderr)
        self.assertEqual(_git(work, "rev-parse", "--short", "HEAD"), old)
        self.assertEqual(_git(work, "rev-parse", "--abbrev-ref", "HEAD"), "v0.5")
        self.assertEqual(self.compose_calls("build"), [])
        self.assertIn("| refused |", self.history(work)[-1])

    def test_meal_window_before_and_after(self):
        work, old, _ = make_repo(self.base)
        self.scripted("health_json", json.dumps({"ok": True, "busy": False, "meal_count": 2,
                                                 "next_meal_in_sec": 300, "last_meal_ago_sec": 30000}))
        r = self.run_pp(work)
        self.assertEqual(r.returncode, 2)
        self.assertIn("next meal is in 5 min", r.stderr)
        self.scripted("health_json", json.dumps({"ok": True, "busy": False, "meal_count": 2,
                                                 "next_meal_in_sec": 30000, "last_meal_ago_sec": 120}))
        r = self.run_pp(work)
        self.assertEqual(r.returncode, 2)
        self.assertIn("a meal was 2 min ago", r.stderr)
        self.assertEqual(self.compose_calls("build"), [])
        # --force overrides the guard
        r = self.run_pp(work, "--latest", "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.compose_calls("build")), 1)
        # Nothing new upstream -> no rebuild, no restart
        self.scripted("health_json", HEALTH_OK)
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Already up to date", r.stdout)
        self.assertEqual(len(self.compose_calls("build")), 1)
        self.assertEqual(len(self.compose_calls("up")), 1)
        self.assertIn("| up_to_date |", self.history(work)[-1])

    def test_v05_log_fallback(self):
        work, old, _ = make_repo(self.base)
        self.scripted("health_json", json.dumps({"ok": True, "status": "idle"}))  # v0.5: no busy / meal fields
        self.scripted("logs", "\n".join([
            "2026-11-05 10:00:00 [INFO] poor_puppy.main: Puppy Feeder Watchdog running (mode=CLOUD). "
            "Monitoring meals: ['07:00', '11:00']",
            "2026-11-05 10:30:00 [INFO] poor_puppy.scheduling: Synced enabled feeding times from feeder DP 207: "
            "['07:00', '18:00']",
        ]))
        today = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        at_0655 = today.replace(hour=6, minute=55).timestamp()
        r = self.run_pp(work, "--latest", PP_NOW_EPOCH=str(at_0655))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("next meal is in 5 min", r.stderr)
        self.assertIn("[logs]", r.stderr)
        # 11:00 is no longer scheduled (last sync wins) -> 10:55 is fine
        at_1055 = today.replace(hour=10, minute=55).timestamp()
        r = self.run_pp(work, "--latest", PP_NOW_EPOCH=str(at_1055))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Branch:     v0.5 -> v0.6", r.stdout)

    def test_unknown_schedule_aborts_non_interactive(self):
        work, old, _ = make_repo(self.base)
        self.scripted("health_json", "")
        self.scripted("logs", "nothing useful here")
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--force", r.stderr)
        self.assertEqual(_git(work, "rev-parse", "--short", "HEAD"), old)

    def test_container_not_running_is_safe(self):
        work, _, _ = make_repo(self.base)
        self.scripted("running", "false")
        self.scripted("health_json", "")
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("not running (nothing to interrupt)", r.stdout)


class TestUpdateFlow(UpdaterTestBase):
    def test_build_failure_restores_git(self):
        work, old, _ = make_repo(self.base)
        self.scripted("build_rc", "1")
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("docker compose build failed", r.stderr)
        self.assertEqual(_git(work, "rev-parse", "--short", "HEAD"), old)
        self.assertEqual(_git(work, "rev-parse", "--abbrev-ref", "HEAD"), "v0.5")
        self.assertEqual((work / "app.txt").read_text(), "v0.5\n")
        self.assertEqual(self.compose_calls("up"), [])
        self.assertEqual(self.compose_calls("run"), [])
        self.assertIn("| build_failed |", self.history(work)[-1])
        # The running image was tagged as the rollback point BEFORE building
        self.assertIn(["tag", OLD_IMAGE_ID, f"poor-puppy-watchdog:rollback-{old}"], self.calls())
        self.assertIn(["tag", OLD_IMAGE_ID, "poor-puppy-watchdog:rollback"], self.calls())

    def test_smoke_failure_keeps_old_container(self):
        work, old, _ = make_repo(self.base)
        self.scripted("smoke_rc", "1")
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Smoke test of the new image failed", r.stderr)
        self.assertEqual(self.compose_calls("up"), [])  # old container never touched
        run = self.compose_calls("run")[0]
        self.assertEqual(run[2:5], ["--rm", "--no-deps", "-T"])
        self.assertEqual(run[5], "--name")
        self.assertTrue(run[6].startswith("poor-puppy-smoke-"))
        self.assertEqual(run[7:10], ["--entrypoint", "python", "feeder-watchdog"])
        self.assertEqual(_git(work, "rev-parse", "--short", "HEAD"), old)
        self.assertEqual(_git(work, "rev-parse", "--abbrev-ref", "HEAD"), "v0.5")
        # The compose image name is pointed back at the old image so `docker compose up -d` stays safe
        self.assertEqual(self.calls()[-1][:1], ["tag"])
        self.assertIn(["tag", f"poor-puppy-watchdog:rollback-{old}", "poor-puppy-feeder-watchdog"], self.calls())
        self.assertIn("| smoke_failed |", self.history(work)[-1])

    def test_selftest_failure_rolls_back_to_tagged_image_on_original_branch(self):
        work, old, old_full = make_repo(self.base)
        self.scripted("selftest.1", SELFTEST_FAIL)
        self.scripted("selftest.2", '{"error": "Not found"}\nHTTP_CODE:404')  # old version has no /selftest
        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("connection self-test failed", r.stderr)
        self.assertIn(f"Rollback to {old} succeeded", r.stdout)
        self.assertEqual(_git(work, "rev-parse", "HEAD"), old_full)
        self.assertEqual(_git(work, "rev-parse", "--abbrev-ref", "HEAD"), "v0.5")  # on the branch, not detached
        ups = self.compose_calls("up")
        self.assertEqual(len(ups), 2)
        self.assertTrue(all("--no-build" in u for u in ups), ups)  # rollback never rebuilds
        calls = self.calls()
        retag = ["tag", f"poor-puppy-watchdog:rollback-{old}", "poor-puppy-feeder-watchdog"]
        self.assertIn(retag, calls)
        up_positions = [i for i, c in enumerate(calls) if c[:2] == ["compose", "up"]]
        self.assertLess(up_positions[0], calls.index(retag))
        self.assertLess(calls.index(retag), up_positions[1])
        # Self-test asked for the camera deep check by default
        selftests = [c for c in calls if c[:2] == ["compose", "exec"] and "/selftest" in " ".join(c)]
        self.assertEqual(selftests[0][-1], "1")
        self.assertIn("| rolled_back |", self.history(work)[-1])
        self.assertIn(f"v0.5@{old} -> v0.6@", self.history(work)[-1])

    def test_health_failure_without_rollback_prints_manual_steps(self):
        work, old, _ = make_repo(self.base)
        self.scripted("health.1", "unhealthy")
        r = self.run_pp(work, "--latest", "--no-rollback", "--no-camera-check")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"git checkout -B v0.5 {old} && update-pp --no-pull", r.stderr)
        self.assertEqual(len(self.compose_calls("up")), 1)
        self.assertEqual(_git(work, "rev-parse", "--abbrev-ref", "HEAD"), "v0.6")
        self.assertIn("| failed_no_rollback |", self.history(work)[-1])

    def test_missing_running_log_line_rolls_back(self):
        work, old, _ = make_repo(self.base)
        self.scripted("logs.1", "Traceback (most recent call last): boom")
        r = self.run_pp(work, "--latest", "--no-camera-check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("never logged 'Puppy Feeder Watchdog running'", r.stderr)
        self.assertIn(f"Rollback to {old} succeeded", r.stdout)

    def test_success_backup_rotation_history_and_status(self):
        from watchdog.db import KibbleDatabase

        work, old, _ = make_repo(self.base)
        db = KibbleDatabase(str(work / "data" / "kibble_history.db"))
        db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, True, False, outcome="dispensed")
        backups = work / "data" / "backups"
        backups.mkdir()
        for i in range(6):
            (backups / f"pre-update-2026010{i}-000000-deadbee.db").write_text("old")
        (backups / "manual-keep.db").write_text("not rotated")

        r = self.run_pp(work, "--latest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Update complete!", r.stdout)
        self.assertIn("Branch:     v0.5 -> v0.6", r.stdout)
        self.assertIn("Self-test:  pass", r.stdout)
        self.assertIn("MOLLY_PASS=keepme", (work / ".env").read_text())
        self.assertIn("UPDATE_GUARD_BEFORE_MIN=10", (work / ".env").read_text())  # backfilled

        kept = sorted(p.name for p in backups.glob("pre-update-*.db"))
        self.assertEqual(len(kept), 5)
        new = [n for n in kept if n.endswith(f"-{old}.db")]
        self.assertEqual(len(new), 1)
        self.assertNotIn("pre-update-20260100-000000-deadbee.db", kept)
        self.assertTrue((backups / "manual-keep.db").exists())
        import sqlite3

        con = sqlite3.connect(str(backups / new[0]))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM feed_events").fetchone()[0], 1)
        con.close()

        # Order: rollback tag -> build -> smoke -> switch -> selftest
        calls = self.calls()

        def first(prefix):
            return next(i for i, c in enumerate(calls) if c[:len(prefix)] == prefix)

        order = [first(["tag", OLD_IMAGE_ID, "poor-puppy-watchdog:rollback"]), first(["compose", "build"]),
                 first(["compose", "run"]), first(["compose", "up"])]
        self.assertEqual(order, sorted(order))
        selftest_idx = next(i for i, c in enumerate(calls) if c[:2] == ["compose", "exec"] and "/selftest" in " ".join(c))
        self.assertGreater(selftest_idx, order[-1])
        self.assertIn("--force-recreate", self.compose_calls("up")[0])

        hist = self.history(work)
        self.assertEqual(len(hist), 1)
        self.assertIn(f"v0.5@{old} -> v0.6@", hist[0])
        self.assertIn("| success |", hist[0])

        s = self.run_pp(work, "--status")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertIn(f"Install dir:   {work}", s.stdout)
        self.assertIn("Branch:        v0.6", s.stdout)
        self.assertIn("Newest branch: v0.6", s.stdout)
        self.assertIn("up to date", s.stdout)
        self.assertIn("Next meal:", s.stdout)
        self.assertIn("| success |", s.stdout)
        self.assertEqual(len(self.history(work)), 1)  # --status never writes history

    def test_status_reports_available_update(self):
        work, _, _ = make_repo(self.base)
        s = self.run_pp(work, "--status", PP_SKIP_DOCKER="1")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertIn("v0.6 is available", s.stdout)
        self.assertIn("(no updates recorded yet)", s.stdout)

    def test_reexec_runs_newly_pulled_updater(self):
        new_script = Path(UPDATER).read_text().replace(
            "main() {\n", 'main() {\n    echo "REEXEC_MARKER pid=$$ reexec=${PP_REEXEC:-0}"\n', 1)
        work, old, _ = make_repo(self.base, v06_updater=new_script)
        r = subprocess.run([UPDATER, "--latest"], env={**os.environ, "POOR_PUPPY_DIR": str(work), "PP_SKIP_DOCKER": "1"},
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("update-pp itself changed", r.stdout)
        self.assertIn("REEXEC_MARKER", r.stdout)
        self.assertIn("reexec=1", r.stdout)
        self.assertIn(f"Commit:     {old} -> ", r.stdout)
        self.assertIn("Branch:     v0.5 -> v0.6", r.stdout)
        self.assertEqual(len(self.history(work)), 1)
        self.assertFalse((work / ".git" / "update-pp.lock").exists())


# --------------------------------------------------------------------------- HTTP endpoints
def _fake_watchdog(td: str):
    import watchdog.main as wm
    from watchdog.config import WatchdogConfig

    w = wm.PuppyFeederWatchdog.__new__(wm.PuppyFeederWatchdog)
    w.cfg = WatchdogConfig()
    w.cfg.meal_preroll_seconds = 40
    w.cfg.ntfy_topic_url = "https://ntfy.example/x"
    w.cfg.discord_webhook_url = w.cfg.telegram_bot_token = w.cfg.ha_webhook_url = ""
    w.cfg.feeder_mode = "cloud"
    w.molly = mock.Mock()
    w.molly.mode = "cloud"
    w.molly.sid, w.molly.uid, w.molly.ecode = "SID123", "UID", "ECODE"
    w.molly.device_name = "Feeder"
    w.molly.is_online = True
    w.molly.cached_dps = {"207": LIVE_SCHEDULE}
    w.molly.refresh_cloud_devices.return_value = True

    def _snap(path, *a, **k):
        Path(path).write_bytes(b"\xff\xd8jpeg")
        return True

    w.molly.capture_snapshot.side_effect = _snap
    import sqlite3

    db_file = Path(td) / "k.db"
    sqlite3.connect(str(db_file)).close()
    w.db = mock.Mock()
    w.db.db_path = db_file
    w.work_dir = Path(td)
    w.latest_snapshot_path = Path(td) / "latest_annotated.jpg"
    w.latest_state = {"status": "idle"}
    w._last_fresh_snapshot = 0.0
    w._busy = threading.Lock()
    w.alerter = mock.Mock()
    return w


class TestHealthAndSelftest(unittest.TestCase):
    def setUp(self):
        import schedule

        self.schedule = schedule
        schedule.clear("meal_checks")
        self._td = tempfile.TemporaryDirectory()
        self.w = _fake_watchdog(self._td.name)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.w.cfg.webhook_listen_host, self.w.cfg.webhook_listen_port = "127.0.0.1", self.port
        self.w.cfg.webhook_secret = "tok"
        self.server = self.w.start_webhook_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.schedule.clear("meal_checks")
        self._td.cleanup()

    def get(self, path, token="tok"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token:
            req.add_header("X-Webhook-Token", token)
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def add_meal(self, minutes_from_now: float):
        t = datetime.now() + timedelta(minutes=minutes_from_now, seconds=-40)
        self.schedule.every().day.at(t.strftime("%H:%M:%S")).do(lambda **k: None, meal_portions=2).tag("meal_checks")
        # pre-meal online check job (not a meal) must be ignored
        self.schedule.every().day.at((t - timedelta(minutes=5)).strftime("%H:%M:%S")).do(
            lambda **k: None, meal_time="x").tag("meal_checks")

    def raw(self, path, token="tok", body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("X-Webhook-Token", token)
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_control_page_needs_token_and_is_served(self):
        self.assertEqual(self.raw("/", token=None)[0], 401)
        code, headers, body = self.raw("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Poor Puppy", body)
        self.assertEqual(self.raw("/ui")[0], 200)

    def test_feed_portions_validated_and_busy_blocks_speaker(self):
        self.assertEqual(self.raw("/webhook/feed", body={"portions": 0})[0], 400)
        self.assertEqual(self.raw("/webhook/feed", body={"portions": 21})[0], 400)
        self.assertEqual(self.raw("/webhook/feed", body={"portions": "x"})[0], 400)
        self.w.molly.dispense_food.assert_not_called()
        with self.w._busy:
            self.assertEqual(self.raw("/webhook/say", body={"text": "hi"})[0], 409)
        self.w.molly.speak_tts.assert_not_called()

    def test_snapshot_never_opens_camera_mid_meal(self):
        with self.w._busy:
            self.assertEqual(self.raw("/snapshot.jpg?fresh=1")[0], 404)  # no old photo, camera not touched
        self.w.molly.capture_snapshot.assert_not_called()
        code, headers, body = self.raw("/snapshot.jpg?fresh=1")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertIn("X-Snapshot-Taken", headers)

    def test_events_timeline_and_photos(self):
        good = Path(self._td.name) / "20261105_160109_after.jpg"
        good.write_bytes(b"\xff\xd8after")
        outside = Path(tempfile.gettempdir()) / "pp_outside_secret.jpg"
        outside.write_bytes(b"\xff\xd8nope")
        self.w.db.get_recent_events.return_value = [
            {"id": 29, "timestamp": "2026-11-05T16:01:09", "event_type": "scheduled_verify", "portions": 20, "success": 1,
             "clog_detected": 0, "outcome": "dispensed", "diagnosis": "a | b", "after_image": str(good),
             "before_image": str(Path(self._td.name) / "gone.jpg"), "annotated_image": None,
             "details_json": json.dumps({"warnings": {"weak_flow": True}, "audio": {"ting_rate_per_sec": 4.1}})},
            {"id": 28, "timestamp": "2026-11-05T11:01:12", "event_type": "scheduled_verify", "portions": 20, "success": 0,
             "clog_detected": 0, "outcome": "recording_cut_short", "after_image": str(outside), "details_json": "{"},
        ]
        code, _, body = self.raw("/events?limit=10")
        self.assertEqual(code, 200)
        evs = json.loads(body)["events"]
        self.assertEqual(evs[0]["photos"], ["after"])           # missing / null photos are not offered
        self.assertEqual(evs[0]["flags"], ["weak_flow"])
        self.assertEqual(evs[1]["photos"], [])                  # outside the captures folder: never served
        code, headers, body = self.raw("/events/29/after.jpg")
        self.assertEqual((code, headers["Content-Type"], body), (200, "image/jpeg", b"\xff\xd8after"))
        self.assertEqual(self.raw("/events/28/after.jpg")[0], 404)
        self.assertEqual(self.raw("/events/29/before.jpg")[0], 404)
        self.assertEqual(self.raw("/events/29/passwd.jpg")[0], 404)
        self.assertEqual(self.raw("/events/x/after.jpg")[0], 400)
        self.assertEqual(self.raw("/events/29/after.jpg", token=None)[0], 401)
        outside.unlink()

    def test_health_reports_busy_and_meal_timing_without_cloud(self):
        code, h = self.get("/health", token=None)
        self.assertEqual(code, 200)
        self.assertEqual((h["busy"], h["meal_count"], h["next_meal_in_sec"]), (False, 0, None))
        self.add_meal(90)
        code, h = self.get("/health", token=None)
        self.assertEqual(h["meal_count"], 1)
        self.assertTrue(89 * 60 <= h["next_meal_in_sec"] <= 91 * 60, h)
        self.assertTrue(86400 - 91 * 60 <= h["last_meal_ago_sec"] <= 86400 - 89 * 60, h)
        with self.w._busy:
            self.assertTrue(self.get("/health", token=None)[1]["busy"])
        self.w.molly.get_status.assert_not_called()
        self.w.molly.refresh_cloud_devices.assert_not_called()

    def test_meal_timing_while_check_is_running(self):
        from watchdog.service.http_api import meal_timing

        self.add_meal(30)
        job = [j for j in self.schedule.get_jobs("meal_checks") if "meal_portions" in j.job_func.keywords][0]
        now = job.next_run + timedelta(seconds=70)  # job fired 70 s ago (40 s pre-roll) and is still recording
        t = meal_timing(40, now=now)
        self.assertEqual(t["last_meal_ago_sec"], 30)
        self.assertEqual(t["next_meal_in_sec"], 86400 - 30)

    def test_selftest_pass_uses_existing_session_only(self):
        self.assertEqual(self.get("/selftest", token=None)[0], 401)
        code, rep = self.get("/selftest?camera=1")
        self.assertEqual(code, 200, rep)
        self.assertEqual(rep["overall"], "pass", rep)
        names = {c["name"]: c["status"] for c in rep["checks"]}
        self.assertEqual(names, {"cloud_api": "pass", "feeder_online": "pass", "schedule_dp207": "pass",
                                 "database": "pass", "alert_channels": "pass", "camera": "pass"})
        self.w.molly.refresh_cloud_devices.assert_called_once_with(allow_relogin=False)
        self.w.molly.login.assert_not_called()
        self.w.molly.get_status.assert_not_called()  # get_status may re-login: never used by the self-test
        self.w.molly.dispense_food.assert_not_called()
        self.w.molly.set_dp.assert_not_called()
        self.w.molly.capture_snapshot.assert_called_once()
        self.assertFalse((Path(self._td.name) / "selftest_snapshot.jpg").exists())
        self.assertFalse(self.w._busy.locked())
        # Without camera=1 no camera session is opened
        self.w.molly.capture_snapshot.reset_mock()
        code, rep = self.get("/selftest")
        self.assertNotIn("camera", {c["name"] for c in rep["checks"]})
        self.w.molly.capture_snapshot.assert_not_called()

    def test_selftest_busy_meal_imminent_offline_and_no_session(self):
        with self.w._busy:
            self.assertEqual(self.get("/selftest")[0], 409)
        self.add_meal(1)
        self.assertEqual(self.get("/selftest")[0], 409)
        self.schedule.clear("meal_checks")

        self.w.molly.is_online = False
        code, rep = self.get("/selftest?camera=1")
        self.assertEqual((code, rep["overall"]), (200, "warn"))
        names = {c["name"]: c["status"] for c in rep["checks"]}
        self.assertEqual(names["feeder_online"], "warn")
        self.assertEqual(names["camera"], "skip")

        self.w.molly.refresh_cloud_devices.reset_mock()
        self.w.molly.sid = ""
        code, rep = self.get("/selftest")
        self.assertEqual((code, rep["overall"]), (503, "fail"))
        self.w.molly.refresh_cloud_devices.assert_not_called()  # never triggers a login
        self.w.molly.login.assert_not_called()

    def test_selftest_cloud_error_is_fail_and_redacts_session(self):
        self.w.molly.refresh_cloud_devices.side_effect = RuntimeError("HTTP 500 for url ...&sid=SID123&x=1")
        code, rep = self.get("/selftest")
        self.assertEqual(code, 503)
        detail = [c for c in rep["checks"] if c["name"] == "cloud_api"][0]["detail"]
        self.assertNotIn("SID123", detail)
        self.assertIn("***", detail)


class TestPpSettingsUpdateEntries(unittest.TestCase):
    def test_update_subcommands_call_update_pp(self):
        import importlib.util

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "update-pp").write_text("#!/bin/sh\n")
            old = os.environ.get("POOR_PUPPY_DIR")
            os.environ["POOR_PUPPY_DIR"] = td
            try:
                spec = importlib.util.spec_from_file_location("pp_upd", ROOT / "scripts" / "pp_settings.py")
                pp = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(pp)
                with mock.patch.object(pp.subprocess, "run") as m_run:
                    m_run.return_value = subprocess.CompletedProcess([], 0)
                    for argv, expected in ((["update"], ["--latest"]), (["update-status"], ["--status"]),
                                           (["selftest"], ["--selftest"])):
                        with mock.patch.object(sys, "argv", ["pp-settings", *argv]):
                            self.assertEqual(pp.main(), 0)
                        cmd = m_run.call_args.args[0]
                        self.assertEqual(cmd[:2], ["bash", str(Path(td) / "update-pp")])
                        self.assertEqual(cmd[2:], expected)
                    with mock.patch("builtins.input", return_value="m"):
                        pp.tools_menu({})
                    self.assertEqual(m_run.call_args.args[0][2:], ["--latest"])
                    with mock.patch("builtins.input", return_value="n"):
                        pp.tools_menu({})
                    self.assertEqual(m_run.call_args.args[0][2:], ["--status"])
            finally:
                if old is None:
                    os.environ.pop("POOR_PUPPY_DIR", None)
                else:
                    os.environ["POOR_PUPPY_DIR"] = old


if __name__ == "__main__":
    unittest.main()


class TestControlPageFeatures(unittest.TestCase):
    """Video/audio with Range, 👍/👎 feedback, labels API (same format as ml/labels), alert history."""

    def setUp(self):
        from watchdog.db import KibbleDatabase

        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.w = _fake_watchdog(self._td.name)
        self.w.db = KibbleDatabase(str(td / "real.db"))
        (td / "c_dispense.mp4").write_bytes(b"0123456789" * 100)
        (td / "c_after.jpg").write_bytes(b"\xff\xd8jpg")
        self.ev = self.w.db.log_feed_event("scheduled_verify", "cloud", "cloud", 20, True, False, outcome="dispensed",
                                           after_image=str(td / "c_after.jpg"), video_clip=str(td / "c_dispense.mp4"),
                                           details={"audio": {"ting_rate_per_sec": 5.5}})
        self.ts = self.w.db.get_recent_events(1)[0]["timestamp"]
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.w.cfg.webhook_listen_host, self.w.cfg.webhook_listen_port, self.w.cfg.webhook_secret = "127.0.0.1", self.port, "tok"
        self.server = self.w.start_webhook_server()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self._td.cleanup()

    def req(self, path, body=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"X-Webhook-Token": "tok", **(headers or {})},
                                   data=None if body is None else json.dumps(body).encode())
        try:
            resp = urllib.request.urlopen(r, timeout=10)
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_video_served_with_range(self):
        code, h, body = self.req(f"/events/{self.ev}/video.mp4")
        self.assertEqual((code, h["Content-Type"], len(body), h["Accept-Ranges"]), (200, "video/mp4", 1000, "bytes"))
        code, h, body = self.req(f"/events/{self.ev}/video.mp4", headers={"Range": "bytes=10-19"})
        self.assertEqual((code, body, h["Content-Range"]), (206, b"0123456789", "bytes 10-19/1000"))
        self.assertEqual(self.req(f"/events/{self.ev}/video.mp4", headers={"Range": "bytes=5000-"})[0], 416)
        self.assertEqual(self.req(f"/events/{self.ev}/audio.wav")[0], 404)  # never captured
        ev = json.loads(self.req("/events")[2])["events"][0]
        self.assertEqual((ev["has_video"], ev["has_audio"], ev["photos"]), (True, False, ["after"]))

    def test_feedback_and_labels_share_one_store(self):
        self.assertEqual(self.req("/webhook/feedback", {"id": 999, "verdict": "right"})[0], 400)
        self.assertEqual(self.req("/webhook/feedback", {"id": self.ev, "verdict": "maybe"})[0], 400)
        self.assertEqual(self.req("/webhook/feedback", {"id": self.ev, "verdict": "wrong"})[0], 200)
        key = f"{self.ts}|{self.ev}"
        self.assertEqual(self.req("/api/label", {"key": key, "label": {"puppy": "at_bowl"}})[0], 200)
        self.assertEqual(self.req("/webhook/feedback", {"id": self.ev, "verdict": "right"})[0], 200)  # merges, keeps puppy
        data = json.loads(self.req("/api/events")[2])
        self.assertEqual(data["labels"][key]["puppy"], "at_bowl")
        self.assertEqual(data["labels"][key]["quick_verdict"], "right")
        self.assertEqual(data["events"][-1]["media"]["video_clip"], f"/events/{self.ev}/video.mp4")
        self.assertIsNone(data["events"][-1]["media"]["audio_clip"])
        self.assertEqual(json.loads(self.req("/events")[2])["events"][0]["feedback"], "right")
        self.assertEqual(self.req("/api/label", {"key": "nokey", "label": {}})[0], 400)

    def test_alert_history_and_label_page(self):
        self.w.db.log_action(action="alert", transport="notify", success=True, summary="CLOG!",
                             payload={"event": "feed_clog_detected", "urgent": True, "message": "m"})
        self.w.db.log_action(action="webhook_say", transport="cloud_imm", success=True, summary="hi")
        alerts = json.loads(self.req("/alerts")[2])["alerts"]
        self.assertEqual([(a["title"], a["urgent"]) for a in alerts], [("CLOG!", True)])
        code, h, body = self.req("/label")
        self.assertEqual(code, 200)
        self.assertIn(b"ML bundle review", body)

    def test_alerter_hook_records_sent_alerts(self):
        from watchdog.alerter import HouseholdAlerter

        a = HouseholdAlerter(self.w.cfg)
        a.cfg.ha_webhook_url = a.cfg.discord_webhook_url = a.cfg.ntfy_topic_url = ""
        seen = []
        a.on_sent = lambda **k: seen.append(k)
        self.assertTrue(a.send_alert("t", "m", event="feed_clog_detected", cooldown_minutes=0))
        self.assertEqual(seen[0]["event"], "feed_clog_detected")


class TestExport(TestControlPageFeatures):
    def test_export_bundle_layout(self):
        import io
        import tarfile

        self.req("/webhook/feedback", {"id": self.ev, "verdict": "right"})
        for media, expect in (("none", set()), ("audio", set()), ("all", {"c_after.jpg", "c_dispense.mp4"})):
            code, h, body = self.req(f"/export?media={media}&days=30")
            self.assertEqual(code, 200)
            self.assertIn("attachment", h["Content-Disposition"])
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
                names = tar.getnames()
                root = names[0].split("/")[0]
                self.assertTrue(root.startswith("poor-puppy-ml-"))
                self.assertIn(f"{root}/manifest.json", names)
                self.assertIn(f"{root}/data/real.db", names)
                labels = json.loads(tar.extractfile(f"{root}/labels/feed_event_labels.json").read())
                self.assertEqual(labels[f"{self.ts}|{self.ev}"]["quick_verdict"], "right")
                self.assertEqual({n.split("/")[-1] for n in names if "/captures/" in n}, expect)
                man = json.loads(tar.extractfile(f"{root}/manifest.json").read())
                self.assertEqual(man["feed_events"]["total"], 1)
        self.assertEqual(self.req("/export?media=bogus")[0], 400)


class TestHaSummary(TestControlPageFeatures):
    def test_summary_is_flat_and_ha_friendly(self):
        code, h, body = self.req("/ha/summary.json")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertEqual((d["last_meal_result"], d["last_meal_ok"], d["last_meal_portions"]), ("fed", True, 20))
        self.assertEqual((d["today_meals"], d["today_fed"], d["today_problems"], d["today_portions"]), (1, 1, 0, 20))
        self.assertRegex(d["last_meal_time"], r"[+-]\d\d:\d\d$")  # timezone-aware (HA timestamp sensors need it)
        self.assertEqual(d["kibble_flow"], 5.5)
        self.assertIsNone(d["hopper_percent"])                     # unknown until a refill is recorded
        self.assertEqual(d["status"], "ok")
        self.assertEqual(self.req("/ha/summary.json", headers={"X-Webhook-Token": "bad"})[0], 401)
        code, h, body = self.req("/ha")
        self.assertEqual(code, 200)
        self.assertIn(b"Home Assistant setup", body)
