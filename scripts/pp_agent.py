#!/usr/bin/env python3
"""pp_agent.py — host-side helper for the web Settings page (runs as root via systemd, stdlib only).

The web UI lives inside the watchdog container and must never get Docker/root access to the host. Instead it drops a
small JSON request into data/agent/requests/; the systemd path unit `pp-agent.path` starts this script, which performs
ONLY the whitelisted actions below with validated values, then writes data/agent/results/<id>.json.

  apply_settings  {values: {KEY: value}, clear: [KEY]}   whitelisted .env keys only -> restart via update-pp --no-pull
  rotate_token    {}                                      new WEBHOOK_SECRET (.env + nginx snippet) -> nginx -t/reload -> restart
  set_password    {username, hash}                        SHA-512 crypt hash computed in the container (never the password)
  update          {}                                      update-pp --latest   (meal guard + build + self-test + rollback)
  selftest        {}                                      update-pp --selftest
  status          {}                                      update-pp --status
  restart         {}                                      update-pp --no-pull
  refresh         {}                                      re-write data/agent/settings.json (masked values + schema)

Usage: pp_agent.py --process   (systemd)   |   pp_agent.py --refresh   (vps_install.sh)
"""
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(os.environ.get("POOR_PUPPY_DIR", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # pp_settings.py lives next to this file
import pp_settings as pps  # noqa: E402  (read_env / write_env / SECRET_KEYS)

AGENT = PROJECT / "data" / "agent"
REQ, RES = AGENT / "requests", AGENT / "results"
NGINX_TOKEN = Path("/etc/nginx/snippets/poor-puppy-token.conf")
HTPASSWD = Path("/etc/nginx/.htpasswd-poor-puppy")
UPDATE_PP = PROJECT / "update-pp"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CONTAINER_UID = 1000

URL = r"https?://[^\s\"'<>]{3,500}"
SCHEMA = [
    ("Alerts", [
        ("ALERT_MODE", "choice", "When to alert", {"choices": ["every_miss", "no_food_hours"]}),
        ("NO_FOOD_ALERT_HOURS", "float", "Quiet mode: alert only if no food seen for this many hours", {"min": 1, "max": 48}),
        ("NO_TING_ESCALATE_AFTER", "int", "Escalate to an urgent alert after this many missed feeds in a row", {"min": 1, "max": 10}),
        ("WEAK_FLOW_TING_RATE", "float", "'Weak kibble flow' warning below this many hits/s", {"min": 0, "max": 20}),
        ("HA_NOTIFY_ON_SUCCESS", "bool", "Also send Home Assistant a message for successful feeds", {}),
    ]),
    ("Alert channels", [
        ("HA_WEBHOOK_URL", "url", "Home Assistant webhook URL", {"secret": True}),
        ("NTFY_TOPIC_URL", "url", "ntfy topic URL", {"secret": True}),
        ("DISCORD_WEBHOOK_URL", "url", "Discord webhook URL", {"secret": True}),
        ("TELEGRAM_BOT_TOKEN", "text", "Telegram bot token", {"secret": True, "pattern": r"[0-9]{5,}:[A-Za-z0-9_-]{20,}"}),
        ("TELEGRAM_CHAT_ID", "text", "Telegram chat ID", {"pattern": r"-?[0-9]{3,20}"}),
        ("HEARTBEAT_URL", "url", "Heartbeat / dead-man ping URL (e.g. healthchecks.io)", {"secret": True}),
    ]),
    ("Voice on the feeder speaker", [
        ("TTS_ALERT_ON_CLOG", "bool", "Speak an alert on the feeder when a clog is detected", {}),
        ("TTS_VOLUME", "float", "Voice volume", {"min": 0.5, "max": 6}),
        ("TTS_REPEAT", "int", "Repeat the voice alert", {"min": 1, "max": 5}),
    ]),
    ("Clog retries", [
        ("AUTO_RETRY_ON_CLOG", "bool", "Automatically retry 1 portion after a clog", {}),
        ("MAX_DAILY_RETRY_PORTIONS", "int", "Max automatic retry portions per day", {"min": 0, "max": 10}),
    ]),
    ("Time", [
        ("TZ", "text", "Timezone (e.g. America/New_York)", {"pattern": r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", "required": True}),
        ("DAILY_SUMMARY_TIME", "hhmm", "Daily summary at (HH:MM, empty = off)", {"empty_ok": True}),
    ]),
    ("Hopper", [
        ("HOPPER_CAPACITY_PORTIONS", "int", "Portions in a full hopper", {"min": 10, "max": 2000}),
        ("HOPPER_LOW_PERCENT", "int", "Low-hopper alert at (%)", {"min": 1, "max": 90}),
    ]),
    ("Footage & data", [
        ("KEEP_FOOTAGE", "bool", "Keep feed videos/photos (off = delete right after analysis)", {}),
        ("CAPTURE_RETENTION_DAYS", "int", "Keep normal footage for (days)", {"min": 1, "max": 365}),
        ("KEEP_FAILED_FOOTAGE_DAYS", "int", "Keep footage of problem feeds for (days)", {"min": 1, "max": 3650}),
        ("DB_RETENTION_DAYS", "int", "Keep feed records for (days, 0 = forever)", {"min": 0, "max": 3650}),
    ]),
]
FIELDS = {key: (kind, label, opts) for _, rows in SCHEMA for key, kind, label, opts in rows}
# What the watchdog uses when a key isn't in .env (watchdog/config.py), shown as "default" on the page
EFFECTIVE_DEFAULTS = {
    "ALERT_MODE": "every_miss", "NO_FOOD_ALERT_HOURS": "8", "NO_TING_ESCALATE_AFTER": "2", "WEAK_FLOW_TING_RATE": "4.6",
    "HA_NOTIFY_ON_SUCCESS": "true", "TTS_ALERT_ON_CLOG": "true", "TTS_VOLUME": "2.5", "TTS_REPEAT": "2",
    "AUTO_RETRY_ON_CLOG": "true", "MAX_DAILY_RETRY_PORTIONS": "3", "DAILY_SUMMARY_TIME": "21:00",
    "HOPPER_CAPACITY_PORTIONS": "240", "HOPPER_LOW_PERCENT": "20", "KEEP_FOOTAGE": "true",
    "CAPTURE_RETENTION_DAYS": "30", "KEEP_FAILED_FOOTAGE_DAYS": "90", "DB_RETENTION_DAYS": "0",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate(key: str, raw) -> str:
    kind, label, o = FIELDS[key]
    v = str(raw if raw is not None else "").strip()
    if "\n" in v or "\r" in v or len(v) > 600:
        raise ValueError(f"{label}: invalid value")
    if v == "" and (o.get("empty_ok") or (kind in ("url", "text") and not o.get("required"))):
        return ""
    if kind == "bool":
        if v.lower() not in ("true", "false"):
            raise ValueError(f"{label}: must be on/off")
        return v.lower()
    if kind in ("int", "float"):
        try:
            n = int(v) if kind == "int" else float(v)
        except ValueError:
            raise ValueError(f"{label}: must be a number")
        if not o.get("min", -1e18) <= n <= o.get("max", 1e18):
            raise ValueError(f"{label}: must be between {o.get('min')} and {o.get('max')}")
        return v
    if kind == "choice":
        if v not in o["choices"]:
            raise ValueError(f"{label}: must be one of {', '.join(o['choices'])}")
        return v
    if kind == "hhmm":
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError(f"{label}: use HH:MM (24 h)")
        return v
    if kind == "url":
        if not re.fullmatch(URL, v):
            raise ValueError(f"{label}: must be an http(s) URL")
        return v
    if kind == "text" and o.get("pattern") and not re.fullmatch(o["pattern"], v):
        raise ValueError(f"{label}: invalid format")
    return v


def chown_for_container(p: Path) -> None:
    try:
        os.chown(p, CONTAINER_UID, CONTAINER_UID)
    except OSError:
        pass


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o644)
    tmp.replace(path)
    chown_for_container(path)


def masked(key: str, val: str) -> str:
    if not val:
        return ""
    return "••••" + val[-4:] if len(val) > 4 else "••••"


def refresh() -> dict:
    env = {**pps.DEFAULTS, **pps.read_env(pps.EXAMPLE), **pps.read_env(pps.ENV_FILE)}
    values = {}
    for key, (kind, label, o) in FIELDS.items():
        v = env.get(key, "")
        values[key] = {"set": bool(v), "masked": masked(key, v)} if o.get("secret") else v
    user = ""
    if HTPASSWD.is_file():
        user = HTPASSWD.read_text().split(":", 1)[0].strip()
    git = lambda *a: subprocess.run(["git", "-C", str(PROJECT), *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    snap = {
        "updated": now(),
        "schema": [{"group": g, "fields": [{"key": k, "kind": kd, "label": lb, "default": EFFECTIVE_DEFAULTS.get(k, ""),
                                            **{x: y for x, y in o.items() if x != "pattern"}}
                                           for k, kd, lb, o in rows]} for g, rows in SCHEMA],
        "values": values,
        "login_user": user,
        "version": {"branch": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": git("rev-parse", "--short", "HEAD"),
                    "date": git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")},
    }
    write_json(AGENT / "settings.json", snap)
    return snap


def run_update_pp(*args: str, timeout: int = 1800) -> tuple:
    env = {**os.environ, "POOR_PUPPY_DIR": str(PROJECT), "TERM": "dumb"}
    p = subprocess.run([str(UPDATE_PP), *args], cwd=PROJECT, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, env=env)
    out = ANSI.sub("", (p.stdout or "") + ("\n" + p.stderr if p.stderr else ""))
    return p.returncode == 0, out


def backup(path: Path, keep: int = 5) -> Path:
    b = path.with_name(f"{path.name}.bak-agent-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, b)
    olds = sorted(path.parent.glob(f"{path.name}.bak-agent-*"))
    for o in olds[:-keep]:
        o.unlink(missing_ok=True)
    return b


def do_apply_settings(params: dict) -> tuple:
    values, clear = params.get("values") or {}, params.get("clear") or []
    if not isinstance(values, dict) or not isinstance(clear, list):
        raise ValueError("bad request")
    updates = {}
    for k, v in values.items():
        if k not in FIELDS:
            raise ValueError(f"{k} can't be changed from the web page")
        if FIELDS[k][2].get("secret") and str(v).strip() == "":
            continue  # empty secret field = keep the current value
        updates[k] = validate(k, v)
    for k in clear:
        if k not in FIELDS or not FIELDS[k][2].get("secret"):
            raise ValueError(f"{k} can't be cleared")
        updates[k] = ""
    if not updates:
        return True, "Nothing changed."
    b = backup(pps.ENV_FILE)
    pps.write_env(updates)
    ok, out = run_update_pp("--no-pull")
    if not ok:
        shutil.copy2(b, pps.ENV_FILE)
        ok2, out2 = run_update_pp("--no-pull")
        return False, (f"Applying failed, so the previous settings were restored (backup {b.name}).\n\n{out}\n\n"
                       f"--- restore ---\n{out2}")
    changed = ", ".join(sorted(updates))
    return True, f"Saved and applied: {changed}\n(backup of the old .env: {b.name})\n\n{out}"


def do_rotate_token(_: dict) -> tuple:
    new = secrets.token_hex(32)
    env_b = backup(pps.ENV_FILE)
    snip_b = backup(NGINX_TOKEN) if NGINX_TOKEN.is_file() else None
    pps.write_env({"WEBHOOK_SECRET": new})
    if NGINX_TOKEN.is_file():
        marker = "# Managed by poor-puppy vps_install.sh"
        NGINX_TOKEN.write_text(f'{marker}\nproxy_set_header X-Webhook-Token "{new}";\n')
        os.chmod(NGINX_TOKEN, 0o600)
        t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if t.returncode != 0:
            shutil.copy2(env_b, pps.ENV_FILE)
            shutil.copy2(snip_b, NGINX_TOKEN)
            return False, "nginx rejected the new config; nothing changed.\n" + t.stderr
        subprocess.run(["systemctl", "reload", "nginx"], check=False)
    ok, out = run_update_pp("--no-pull")
    if not ok:
        shutil.copy2(env_b, pps.ENV_FILE)
        if snip_b:
            shutil.copy2(snip_b, NGINX_TOKEN)
            subprocess.run(["systemctl", "reload", "nginx"], check=False)
        run_update_pp("--no-pull")
        return False, "Restart with the new token failed, so the old token was restored.\n\n" + out
    return True, "API token rotated (64 random hex chars). nginx and the watchdog now use the new one; the old one no longer works.\n\n" + out


def do_set_password(params: dict) -> tuple:
    user, h = str(params.get("username") or ""), str(params.get("hash") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", user):
        raise ValueError("username must be 3-32 letters/digits/._-")
    if not re.fullmatch(r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}", h):
        raise ValueError("bad password hash")
    if HTPASSWD.is_file():
        backup(HTPASSWD)
    HTPASSWD.write_text(f"{user}:{h}\n")
    try:
        shutil.chown(HTPASSWD, "root", "www-data")
    except (LookupError, OSError):
        pass
    os.chmod(HTPASSWD, 0o640)
    return True, f"Login changed for '{user}'. Your browser will ask you to log in again. Update Home Assistant's secrets.yaml too if you use it."


ACTIONS = {
    "apply_settings": do_apply_settings,
    "rotate_token": do_rotate_token,
    "set_password": do_set_password,
    "update": lambda p: run_update_pp("--latest"),
    "selftest": lambda p: run_update_pp("--selftest", timeout=300),
    "status": lambda p: run_update_pp("--status", timeout=300),
    "restart": lambda p: run_update_pp("--no-pull"),
    "refresh": lambda p: (True, "Settings snapshot refreshed."),
}


def process() -> int:
    for d in (AGENT, REQ, RES):
        d.mkdir(parents=True, exist_ok=True)
        chown_for_container(d)
    with open(AGENT / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for req_file in sorted(REQ.glob("*.json")):
            rid = req_file.stem
            try:
                req = json.loads(req_file.read_text())
            except (OSError, ValueError):
                req = {}
            req_file.unlink(missing_ok=True)
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", rid):
                continue
            action = req.get("action")
            started = now()
            write_json(RES / f"{rid}.json", {"id": rid, "action": action, "status": "running", "started": started})
            try:
                if action not in ACTIONS:
                    raise ValueError(f"unknown action {action!r}")
                ok, out = ACTIONS[action](req.get("params") or {})
            except subprocess.TimeoutExpired:
                ok, out = False, "Timed out."
            except Exception as exc:
                ok, out = False, str(exc)
            write_json(RES / f"{rid}.json", {"id": rid, "action": action, "status": "done", "ok": bool(ok),
                                             "output": out[-12000:], "started": started, "finished": now()})
            try:
                refresh()
            except Exception:
                pass
        results = sorted(RES.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in results[:-50]:
            old.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("pp_agent.py must run as root (it is started by systemd: pp-agent.path)")
    if "--refresh" in sys.argv:
        for d in (AGENT, REQ, RES):
            d.mkdir(parents=True, exist_ok=True)
            chown_for_container(d)
        refresh()
        print(f"wrote {AGENT / 'settings.json'}")
        sys.exit(0)
    sys.exit(process())
