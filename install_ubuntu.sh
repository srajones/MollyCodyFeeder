#!/usr/bin/env bash
# ============================================================================
# Poor Puppy (Molly & Cody Smart Feeder Watchdog) — Ubuntu VPS installer
#   ./install_ubuntu.sh        install / update and start the watchdog
# Afterwards:  pp-settings     numbered settings menu
#              update-pp       safe updates (see README); re-running this script on an existing install is safe:
#                              it never overwrites .env values, data/, captures/ or tuya_session.json, and hands
#                              the rebuild to `update-pp --no-pull` (meal-window guard, smoke test, auto-rollback).
# Outbound-only: no ports are opened on the VPS.
# ============================================================================
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DIR="$(pwd)"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say()  { printf '\n\033[1;36m🐶 %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*"; }

EXISTING_INSTALL=0
[ -f .env ] && [ -d data ] && EXISTING_INSTALL=1
say "Poor Puppy install directory: ${DIR}"
[ "$EXISTING_INSTALL" -eq 1 ] && echo "   Existing install detected — .env, data/, captures/ and tuya_session.json are kept as-is."

# 1. Docker Engine + compose plugin --------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker Engine..."
    $SUDO apt-get update -y
    $SUDO apt-get install -y ca-certificates curl
    curl -fsSL https://get.docker.com | $SUDO sh
    $SUDO usermod -aG docker "${SUDO_USER:-$USER}" || true
fi
if ! $SUDO docker compose version >/dev/null 2>&1; then
    say "Installing the docker compose plugin..."
    $SUDO apt-get install -y docker-compose-plugin
fi
command -v python3 >/dev/null 2>&1 || $SUDO apt-get install -y python3   # for pp-settings (stdlib only)

# 2. Configuration ---------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    warn "Created .env from .env.example"
fi
# Ensure retention, ML export, and outbound-only defaults exist in .env without overwriting user customizations
ensure_default() {
    local k="$1" v="$2"
    if ! grep -qE "^[[:space:]]*${k}=" .env; then
        echo "${k}=${v}" >> .env
    fi
}
ensure_default KEEP_FOOTAGE true
ensure_default CAPTURE_RETENTION_DAYS 30
ensure_default KEEP_FAILED_FOOTAGE_DAYS 90
ensure_default VIDEO_KEEP_DAYS_OK 0
ensure_default CAPTURE_MAX_MB 2048
ensure_default DB_RETENTION_DAYS 0
ensure_default EXPORT_ML_ON_PRUNE true
ensure_default ML_EXPORT_PATH data/ml_events.jsonl
ensure_default WEBHOOK_LISTEN_HOST 127.0.0.1
ensure_default HA_NOTIFY_ON_SUCCESS true
# Timezone: the feeder's meal times are local; default to the VPS timezone if not set
if ! grep -qE '^TZ=.+' .env; then
    TZV="$(timedatectl show -p Timezone --value 2>/dev/null || echo America/New_York)"
    if grep -qE '^TZ=' .env; then sed -i "s|^TZ=.*|TZ=${TZV}|" .env; else echo "TZ=${TZV}" >> .env; fi
    warn "TZ set to ${TZV} — change it with pp-settings if your feeder is in another timezone"
fi
if [ ! -f tuya_session.json ] && ! grep -qE '^MOLLY_USER=.+' .env && ! grep -qE '^USER=.+@' .env; then
    warn "No tuya_session.json and no MOLLY_USER/MOLLY_PASS in .env."
    echo "   Copy the session from your Mac:   scp tuya_session.json <vps>:${DIR}/"
    echo "   or put MOLLY_USER= / MOLLY_PASS= in .env, then re-run this script."
    exit 1
fi
[ -f tuya_session.json ] || echo '{}' > tuya_session.json

# 3. State directories (the container runs as uid 1000) ---------------------------
mkdir -p data data/backups captures captures/review_clips
$SUDO chown -R 1000:1000 data captures tuya_session.json 2>/dev/null || true
chmod 600 tuya_session.json || true

# 4. Build & start ------------------------------------------------------------------
chmod +x update-pp 2>/dev/null || true
if [ "$($SUDO docker inspect -f '{{.State.Running}}' poor-puppy-watchdog 2>/dev/null || true)" = "true" ]; then
    # Re-run on a live install: use the safe updater (meal-window guard, build first, smoke test, DB backup,
    # connection self-test, automatic rollback) instead of a blind rebuild + restart
    say "Watchdog already running — rebuilding safely via update-pp --no-pull..."
    if ! POOR_PUPPY_DIR="$DIR" ./update-pp --no-pull; then
        warn "update-pp did not switch versions (see messages above); the previous container is still running."
    fi
else
    say "Building and starting the watchdog container..."
    $SUDO docker compose up -d --build

    say "Waiting for the health check (up to ~3.5 intervals / 210s)..."
    STATUS="starting"
    for _ in $(seq 1 70); do
        STATUS="$($SUDO docker inspect -f '{{.State.Health.Status}}' poor-puppy-watchdog 2>/dev/null || echo missing)"
        [ "$STATUS" = "healthy" ] && break
        sleep 3
    done
    if [ "$STATUS" = "healthy" ]; then
        echo "   ✅ poor-puppy-watchdog is healthy"
    else
        warn "Container status: ${STATUS}. Last log lines:"
        $SUDO docker compose logs --tail 30 feeder-watchdog || true
    fi
fi

# 5. pp-settings & update-pp commands -----------------------------------------------
$SUDO tee /usr/local/bin/pp-settings >/dev/null <<EOF
#!/bin/sh
POOR_PUPPY_DIR="${DIR}" exec python3 "${DIR}/scripts/pp_settings.py" "\$@"
EOF
$SUDO chmod 755 /usr/local/bin/pp-settings

$SUDO tee /usr/local/bin/update-pp >/dev/null <<EOF
#!/bin/sh
POOR_PUPPY_DIR="${DIR}" exec "${DIR}/update-pp" "\$@"
EOF
$SUDO chmod 755 /usr/local/bin/update-pp

say "Done!"
echo "   Installed at:   ${DIR}"
echo "   Settings menu:  pp-settings   (alerts, HA_WEBHOOK_URL, footage retention & ML export)"
echo "   Safe updater:   update-pp --latest   (or pp-settings -> 8) Tools -> m) Update; build, test, auto-rollback)"
echo "   Update status:  update-pp --status   (version, available updates, health, next meal, history)"
echo "   Comms check:    update-pp --selftest (cloud session, feeder online, schedule, DB, alerts, camera; no feed)"
echo "   View schedule:  pp-settings schedule        (docker compose exec feeder-watchdog python feeder_cli.py schedule list)"
echo "   Feed (fast):    pp-settings feed [N]        (docker compose exec feeder-watchdog python feeder_cli.py feed N)"
echo "   Feed + verify:  pp-settings feed-verify [N] (docker compose exec feeder-watchdog python feeder_cli.py feed N --verify)"
echo "   Speak (TTS):    pp-settings say \"<text>\"    (or pp-settings -> 8) Tools -> e) Speak (custom TTS) / f) TTS test / g) Speak last alert)"
echo "   Export ML data: ./scripts/export_ml_bundle.sh  (default: DB + JSONL + captures; --no-media for metrics only)"
echo "   Retention:      KEEP_FOOTAGE=true, CAPTURE_RETENTION_DAYS=30, KEEP_FAILED_FOOTAGE_DAYS=90,"
echo "                   DB_RETENTION_DAYS=0 (metrics kept forever + exported to data/ml_events.jsonl)"
echo "   Network:        100% outbound-only (zero host ports published)"
echo "   Live logs:      $SUDO docker compose -f ${DIR}/docker-compose.yml logs -f"
if [ -t 0 ]; then
    read -r -p "   Open pp-settings now to configure alerts / HA_WEBHOOK_URL / retention? (y/n) " ans
    case "$ans" in y*|Y*) pp-settings ;; esac
fi
