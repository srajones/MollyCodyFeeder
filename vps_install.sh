#!/usr/bin/env bash
# =====================================================================================================
# vps_install.sh — add the Poor Puppy web control page (photo / speak / feed / bowl check) on a subdomain
#
#   sudo ./vps_install.sh --check              Preflight only: inspects everything, changes NOTHING
#   sudo ./vps_install.sh                      Install / repair (asks before every change)
#   sudo ./vps_install.sh --uninstall          Remove the page again (keeps the certificate)
#   Options: --domain NAME (default poor-puppy.epicfury4d.live)   --yes (don't ask)
#
# Designed for a VPS that already runs other nginx sites and docker compose projects. It ONLY touches:
#   /opt/poor-puppy/docker-compose.override.yml        (git-ignored; publishes the API on 127.0.0.1:<free port>)
#   /opt/poor-puppy/.env                               (adds WEBHOOK_SECRET if missing; backup first)
#   /etc/nginx/sites-available/<domain> + its symlink  (a NEW file only for <domain>)
#   /etc/nginx/snippets/poor-puppy-token.conf          (API token for nginx, root-only)
#   /etc/nginx/.htpasswd-poor-puppy                    (your login)
#   a Let's Encrypt certificate for <domain>           (same certbot + auto-renewal as your other sites)
# It never edits other nginx files, never restarts nginx (only `reload` after `nginx -t` passes), never runs
# `docker compose down`, never touches other compose projects, and refuses to run if anything looks off.
# =====================================================================================================
set -Eeuo pipefail

DOMAIN="poor-puppy.epicfury4d.live"
MODE="install"
ASSUME_YES=0
PORT_RANGE_START=8090
PORT_RANGE_END=8199
MARKER="# Managed by poor-puppy vps_install.sh"

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
        --check) MODE="check"; shift ;;
        --uninstall) MODE="uninstall"; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SITE_AVAIL="/etc/nginx/sites-available/${DOMAIN}"
SITE_ENABLED="/etc/nginx/sites-enabled/${DOMAIN}"
TOKEN_SNIPPET="/etc/nginx/snippets/poor-puppy-token.conf"
HTPASSWD="/etc/nginx/.htpasswd-poor-puppy"
OVERRIDE="${DIR}/docker-compose.override.yml"
LE_LIVE="/etc/letsencrypt/live/${DOMAIN}"
TS="$(date +%Y%m%d-%H%M%S)"
CHANGES=()

c_step() { printf '\n\033[1;36m━━ %s\033[0m\n' "$*"; }
c_info() { printf '   %s\n' "$*"; }
c_ok()   { printf '   \033[1;32m✔\033[0m %s\n' "$*"; }
c_warn() { printf '   \033[1;33m⚠\033[0m %s\n' "$*"; }
die()    { printf '\n   \033[1;31m✘ %s\033[0m\n' "$*" >&2; summary_changes; exit 1; }
summary_changes() {
    if [ ${#CHANGES[@]} -gt 0 ]; then
        printf '\n   Changes made so far (each has a backup or is safe to keep):\n'
        printf '     - %s\n' "${CHANGES[@]}"
        printf '   Undo everything with:  sudo %s --uninstall\n' "$0"
    else
        printf '   Nothing was changed on this server.\n'
    fi
}
trap 'die "Unexpected error on line ${LINENO} (command: ${BASH_COMMAND}). Stopped safely."' ERR

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    local ans
    read -r -p "   → $1 [y/N] " ans </dev/tty || return 1
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

port_in_use() {  # listening right now, reserved by any container, or referenced in another nginx site
    local p="$1"
    if ss -ltnH "( sport = :${p} )" 2>/dev/null | grep -q .; then return 0; fi
    if docker ps -a --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -v '^poor-puppy-watchdog ' | grep -Eq "[:.]${p}->"; then return 0; fi
    local ng_hits
    ng_hits="$(grep -RlsE "[:.]${p}([^0-9]|$)" /etc/nginx/sites-available/ /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
        | while read -r f; do [ "$(readlink -f "$f")" = "$(readlink -f "$SITE_AVAIL" 2>/dev/null || echo none)" ] || echo "$f"; done || true)"
    if [ -n "$ng_hits" ]; then return 0; fi
    return 1
}

current_override_port() {
    [ -f "$OVERRIDE" ] && grep -Eo '127\.0\.0\.1:[0-9]+:8080' "$OVERRIDE" | head -1 | cut -d: -f2 || true
}

reload_nginx_safely() {  # $1 = what to restore if the test fails (function name)
    if nginx -t 2>/tmp/pp-nginx-test.log; then
        systemctl reload nginx
        c_ok "nginx config test passed → reloaded (no restart; other sites keep serving)"
    else
        c_warn "nginx config test FAILED — your running nginx was NOT reloaded:"
        sed 's/^/      /' /tmp/pp-nginx-test.log
        "$1"
        nginx -t >/dev/null 2>&1 && c_ok "Rolled back; nginx config is valid again (nothing was reloaded)."
        die "nginx rejected the new site config. Rolled back."
    fi
}

compose() { (cd "$DIR" && docker compose "$@"); }

wait_healthy() {
    local s="starting"
    for _ in $(seq 1 70); do
        s="$(docker inspect -f '{{.State.Health.Status}}' poor-puppy-watchdog 2>/dev/null || echo missing)"
        [ "$s" = "healthy" ] && return 0
        sleep 3
    done
    c_warn "container status: ${s}"
    return 1
}

env_secret() { grep -E '^WEBHOOK_SECRET=' "${DIR}/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'" ; }

# =====================================================================================================
c_step "Poor Puppy web control page — mode: ${MODE}, domain: ${DOMAIN}"
[ "$(id -u)" -eq 0 ] || die "Run as root (sudo): nginx and certbot need it."
[ -f "${DIR}/docker-compose.yml" ] && [ -f "${DIR}/.env" ] && [ -x "${DIR}/update-pp" ] \
    || die "Run this from the Poor Puppy install folder (expected docker-compose.yml, .env, update-pp in ${DIR})."
c_ok "Install folder: ${DIR}"

# ---------------------------------------------------------------------------------------- uninstall
if [ "$MODE" = "uninstall" ]; then
    confirm "Remove the web page (nginx site, login, token, loopback port)? The certificate is kept." || die "Cancelled."
    if [ -L "$SITE_ENABLED" ] || [ -f "$SITE_AVAIL" ]; then
        if [ -f "$SITE_AVAIL" ] && ! grep -qF "$MARKER" "$SITE_AVAIL"; then
            die "${SITE_AVAIL} was not created by this script — refusing to delete it."
        fi
        rm -f "$SITE_ENABLED"
        [ -f "$SITE_AVAIL" ] && mv "$SITE_AVAIL" "${SITE_AVAIL}.removed-${TS}"
        restore_none() { :; }
        reload_nginx_safely restore_none
        c_ok "nginx site removed (backup: ${SITE_AVAIL}.removed-${TS})"
    fi
    rm -f "$TOKEN_SNIPPET" "$HTPASSWD" && c_ok "Removed nginx token snippet and login file"
    if [ -f /etc/systemd/system/pp-agent.path ]; then
        systemctl disable --now pp-agent.path >/dev/null 2>&1 || true
        rm -f /etc/systemd/system/pp-agent.path /etc/systemd/system/pp-agent.service
        systemctl daemon-reload
        c_ok "Removed the settings helper (pp-agent)"
    fi
    if [ -f "$OVERRIDE" ]; then
        mv "$OVERRIDE" "${OVERRIDE}.removed-${TS}"
        compose up -d --no-build feeder-watchdog
        wait_healthy && c_ok "Watchdog recreated without the loopback port (healthy)" || c_warn "Check: docker compose ps"
    fi
    c_info "Certificate kept. To delete it too:  certbot delete --cert-name ${DOMAIN}"
    c_ok "Uninstalled."
    exit 0
fi

# ---------------------------------------------------------------------------------------- preflight
c_step "1/9  Preflight checks (read-only)"
for cmd in docker nginx certbot openssl curl ss getent systemctl python3; do
    command -v "$cmd" >/dev/null 2>&1 || die "Missing command: ${cmd}"
done
c_ok "Tools present: docker, nginx, certbot, openssl, curl, ss"
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing"

state="$(docker inspect -f '{{.State.Health.Status}}' poor-puppy-watchdog 2>/dev/null || echo missing)"
[ "$state" = "healthy" ] || die "poor-puppy-watchdog is '${state}', not healthy. Fix that first (update-pp --status)."
c_ok "poor-puppy-watchdog container is healthy"

systemctl is-active --quiet nginx || die "nginx is not running — refusing to touch it."
nginx -t >/dev/null 2>&1 || die "Your CURRENT nginx config already fails 'nginx -t'. Not touching anything; fix that first."
c_ok "nginx is running and the existing config passes 'nginx -t'"

if [ -f "$SITE_AVAIL" ] && ! grep -qF "$MARKER" "$SITE_AVAIL"; then
    die "${SITE_AVAIL} already exists and wasn't created by this script. Refusing to overwrite it."
fi
other_hits="$(grep -RlsE "server_name[^;]*[[:space:]]${DOMAIN//./\\.}([[:space:]]|;)" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
    | while read -r f; do [ "$(readlink -f "$f")" = "$(readlink -f "$SITE_AVAIL" 2>/dev/null || echo none)" ] || echo "$f"; done || true)"
[ -z "$other_hits" ] || die "Another nginx site already serves ${DOMAIN}: ${other_hits}"
zone_hits="$(grep -RlsE 'zone=pp_(page|act|conn|media)[: ]' /etc/nginx/ 2>/dev/null | grep -v "^${SITE_AVAIL}$" || true)"
[ -z "$zone_hits" ] || die "nginx rate-limit zone names pp_page/pp_act/pp_media/pp_conn already used in: ${zone_hits}"
c_ok "No other nginx site uses ${DOMAIN} or our rate-limit zone names"
existing_certs="$(ls -1 /etc/letsencrypt/renewal/*.conf 2>/dev/null | sed 's|.*/||; s|\.conf$||' | grep -vxF "$DOMAIN" | tr '\n' ' ' || true)"
[ -n "$existing_certs" ] && c_info "Existing Let's Encrypt certs on this server (will NOT be touched): ${existing_certs}"

dns_ips="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
my_ips="$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ') $(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
dns_ok=0
for ip in $dns_ips; do case " $my_ips " in *" $ip "*) dns_ok=1 ;; esac; done
if [ -d "$LE_LIVE" ]; then
    c_ok "Certificate for ${DOMAIN} already exists (will be reused)"
elif [ "$dns_ok" -eq 1 ]; then
    c_ok "DNS: ${DOMAIN} → ${dns_ips}(this server)"
else
    die "DNS: ${DOMAIN} resolves to '${dns_ips:-nothing}', not this server (${my_ips}). Create/fix the A record, wait a few minutes, re-run."
fi

existing_port="$(current_override_port)"
if [ -n "$existing_port" ] && docker port poor-puppy-watchdog 8080/tcp 2>/dev/null | grep -q "127.0.0.1:${existing_port}"; then
    PORT="$existing_port"
    c_ok "Re-using the loopback port already assigned to the watchdog: 127.0.0.1:${PORT}"
else
    PORT=""
    for p in $(seq "$PORT_RANGE_START" "$PORT_RANGE_END"); do
        if ! port_in_use "$p"; then PORT="$p"; break; fi
    done
    [ -n "$PORT" ] || die "No free port in ${PORT_RANGE_START}-${PORT_RANGE_END}."
    c_ok "Free loopback port chosen: 127.0.0.1:${PORT} (checked live listeners AND every container's port mappings)"
fi
c_info "Ports currently in use on this VPS (for reference):"
ss -ltnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -n | uniq | tr '\n' ' ' | sed 's/^/      /'; echo

health="$(docker exec poor-puppy-watchdog curl -fsS --max-time 5 http://127.0.0.1:8080/health 2>/dev/null || echo '{}')"
read -r BUSY NEXT_MEAL <<<"$(python3 -c 'import json,sys; d=json.loads(sys.argv[1] or "{}"); print(int(bool(d.get("busy"))), d.get("next_meal_in_sec") if d.get("next_meal_in_sec") is not None else -1)' "$health")"
if [ "$BUSY" = "1" ] || { [ "$NEXT_MEAL" -ge 0 ] && [ "$NEXT_MEAL" -lt 600 ]; }; then
    die "A meal check is running or starts within 10 minutes (the container restarts briefly). Try again after the meal."
fi
c_ok "No meal check within the next 10 minutes"

if [ "$MODE" = "check" ]; then
    c_step "Preflight passed. Nothing was changed (--check)."
    c_info "Run without --check to install. Plan: 127.0.0.1:${PORT} → https://${DOMAIN} (login + rate limit)."
    exit 0
fi

c_step "Plan"
c_info "• Publish the watchdog API on 127.0.0.1:${PORT} only (never on the public interface)"
c_info "• nginx site ${DOMAIN}: HTTPS, login, per-IP rate limits, API token injected server-side"
c_info "• Let's Encrypt certificate for ${DOMAIN} with the same auto-renewal as your other certs"
confirm "Proceed?" || die "Cancelled."

# ---------------------------------------------------------------------------------------- API token
c_step "2/9  API token (WEBHOOK_SECRET)"
if [ -n "$(env_secret)" ]; then
    c_ok "WEBHOOK_SECRET already set in .env (kept)"
else
    cp -p "${DIR}/.env" "${DIR}/.env.bak-vpsinstall-${TS}"
    sed -i '/^WEBHOOK_SECRET=/d' "${DIR}/.env"
    printf 'WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)" >> "${DIR}/.env"
    chmod 600 "${DIR}/.env"
    CHANGES+=("added WEBHOOK_SECRET to .env (backup .env.bak-vpsinstall-${TS})")
    c_ok "Generated a random 64-char WEBHOOK_SECRET (backup: .env.bak-vpsinstall-${TS})"
fi
SECRET="$(env_secret)"

# ---------------------------------------------------------------------------------------- compose override
c_step "3/9  Publish the API on the VPS loopback only (docker-compose.override.yml)"
[ -f "$OVERRIDE" ] && cp -p "$OVERRIDE" "${OVERRIDE}.bak-${TS}"
cat > "$OVERRIDE" <<EOF
${MARKER} — $(date -Is)
# Publishes the watchdog API on this VPS's loopback only, for the nginx control page. Git-ignored.
# Remove with: sudo ./vps_install.sh --uninstall
services:
  feeder-watchdog:
    environment:
      - WEBHOOK_LISTEN_HOST=0.0.0.0
    ports:
      - "127.0.0.1:${PORT}:8080"
EOF
CHANGES+=("wrote ${OVERRIDE}")
compose config >/dev/null || die "docker compose rejected the override file."
c_ok "Override written; 'docker compose config' validates it"

c_step "4/9  Recreate ONLY the poor-puppy-watchdog container (other compose projects untouched)"
restore_override() {
    if [ -f "${OVERRIDE}.bak-${TS}" ]; then mv "${OVERRIDE}.bak-${TS}" "$OVERRIDE"; else rm -f "$OVERRIDE"; fi
    compose up -d --no-build feeder-watchdog >/dev/null 2>&1 || true
}
if ! compose up -d --no-build feeder-watchdog; then restore_override; die "Container failed to start; override reverted."; fi
if ! wait_healthy; then restore_override; die "Container not healthy with the port published; reverted to the previous setup."; fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${PORT}/health" || true)"
[ "$code" = "200" ] || { restore_override; die "API not reachable on 127.0.0.1:${PORT} (HTTP ${code}); reverted."; }
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "X-Webhook-Token: ${SECRET}" "http://127.0.0.1:${PORT}/" || true)"
[ "$code" = "200" ] || { restore_override; die "Control page not served (HTTP ${code}). Is the watchdog on v0.6+? Run update-pp --latest first."; }
code_noauth="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${PORT}/status" || true)"
[ "$code_noauth" = "401" ] || c_warn "API answered ${code_noauth} without a token (expected 401)."
c_ok "Watchdog healthy; API on 127.0.0.1:${PORT}; page OK with token, 401 without"
if ss -ltnH "( sport = :${PORT} )" | awk '{print $4}' | grep -vq '^127\.0\.0\.1:'; then
    die "Port ${PORT} is listening on a non-loopback address — refusing to continue."
fi
c_ok "Confirmed: port ${PORT} listens on 127.0.0.1 only (not reachable from the internet)"

# ---------------------------------------------------------------------------------------- login
c_step "5/9  Login for the page"
if [ -s "$HTPASSWD" ] && { [ "$ASSUME_YES" -eq 1 ] || ! confirm "A login already exists ($(cut -d: -f1 "$HTPASSWD" | tr '\n' ' ')). Replace it?"; }; then
    c_ok "Keeping the existing login"
else
    read -r -p "   Username [puppy]: " PP_USER </dev/tty || true
    PP_USER="${PP_USER:-puppy}"
    [[ "$PP_USER" =~ ^[A-Za-z0-9._-]{3,32}$ ]] || die "Username must be 3-32 letters/digits/._-"
    while true; do
        read -r -s -p "   Password (min 12 chars): " PW1 </dev/tty; echo
        read -r -s -p "   Repeat password: " PW2 </dev/tty; echo
        [ "$PW1" = "$PW2" ] || { c_warn "Passwords don't match, try again."; continue; }
        [ "${#PW1}" -ge 12 ] || { c_warn "Too short (min 12), try again."; continue; }
        break
    done
    HASH="$(printf '%s' "$PW1" | openssl passwd -6 -stdin)"
    unset PW1 PW2
    umask 027
    printf '%s:%s\n' "$PP_USER" "$HASH" > "$HTPASSWD"
    chown root:www-data "$HTPASSWD" 2>/dev/null || true
    chmod 640 "$HTPASSWD"
    CHANGES+=("wrote ${HTPASSWD}")
    c_ok "Login saved for '${PP_USER}' (SHA-512 hash; readable by root + nginx only)"
fi
mkdir -p "$(dirname "$TOKEN_SNIPPET")"
( umask 077; printf '%s\nproxy_set_header X-Webhook-Token "%s";\n' "$MARKER" "$SECRET" > "$TOKEN_SNIPPET" )
chmod 600 "$TOKEN_SNIPPET"
CHANGES+=("wrote ${TOKEN_SNIPPET}")
c_ok "nginx token snippet written (root-only, 600)"

# ---------------------------------------------------------------------------------------- certificate
c_step "6/9  HTTPS certificate"
if [ -d "$LE_LIVE" ]; then
    c_ok "Re-using existing certificate: ${LE_LIVE}"
else
    c_info "Requesting a certificate for ${DOMAIN} (certbot nginx authenticator — same as your other sites;"
    c_info "it only adds a temporary challenge and restores nginx itself)."
    if ! certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --keep-until-expiring \
            --deploy-hook "systemctl reload nginx"; then
        die "certbot could not get a certificate (DNS/rate limit?). No nginx site was added."
    fi
    CHANGES+=("certificate ${DOMAIN} (certbot)")
    c_ok "Certificate issued: ${LE_LIVE}"
fi
nginx -t >/dev/null 2>&1 || die "nginx config is not valid after certbot — stopping (certbot normally restores it)."

# ---------------------------------------------------------------------------------------- nginx site
c_step "7/9  nginx site for ${DOMAIN} (new file only)"
SSL_OPTS=""
[ -f /etc/letsencrypt/options-ssl-nginx.conf ] && SSL_OPTS+="    include /etc/letsencrypt/options-ssl-nginx.conf;"$'\n'
[ -f /etc/letsencrypt/ssl-dhparams.pem ] && SSL_OPTS+="    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"$'\n'
[ -f "$SITE_AVAIL" ] && cp -p "$SITE_AVAIL" "${SITE_AVAIL}.bak-${TS}"
cat > "$SITE_AVAIL" <<EOF
${MARKER} — $(date -Is)
# Poor Puppy control page → watchdog API on 127.0.0.1:${PORT}. Regenerate: sudo ${DIR}/vps_install.sh
# Optional IP allow-list: add "allow <your-ip>; deny all;" inside the 443 server block.

limit_req_zone \$binary_remote_addr zone=pp_page:10m rate=30r/m;   # page, status, photos
limit_req_zone \$binary_remote_addr zone=pp_act:10m  rate=6r/m;    # feed / speak / bowl check / refill / feedback
limit_req_zone \$binary_remote_addr zone=pp_media:10m rate=240r/m;  # past feeds' photos / video / audio
limit_conn_zone \$binary_remote_addr zone=pp_conn:10m;

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${DOMAIN};

    ssl_certificate ${LE_LIVE}/fullchain.pem;
    ssl_certificate_key ${LE_LIVE}/privkey.pem;
${SSL_OPTS}
    # limit_req runs before the login check, so it also throttles password guessing
    limit_conn pp_conn 10;
    limit_req_status 429;
    client_max_body_size 16k;

    auth_basic "Poor Puppy";
    auth_basic_user_file ${HTPASSWD};

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header X-Robots-Tag "noindex, nofollow" always;

    # Never expose self-test / schedule-editing / mode endpoints publicly
    location ~ ^/(selftest|webhook/(schedule|mode)) { return 403; }

    # Actions that move food / use the speaker / reset the hopper / save feedback: strict limit
    location /webhook/ {
        limit_req zone=pp_act burst=3 nodelay;
        include ${TOKEN_SNIPPET};
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 90s;
    }

    # Labelling page saves (one POST per labelled feed)
    location /api/ {
        limit_req zone=pp_page burst=20 nodelay;
        limit_except GET POST { deny all; }
        include ${TOKEN_SNIPPET};
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
    }

    # Data export for analysis: streamed .tar.gz (can be large; don't buffer it to disk, allow time)
    location = /export {
        limit_req zone=pp_act burst=2 nodelay;
        limit_except GET { deny all; }
        include ${TOKEN_SNIPPET};
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_read_timeout 900s;
    }

    # Recorded photos / video / audio of past feeds (video seeking makes many small range requests)
    location /events/ {
        limit_req zone=pp_media burst=60 nodelay;
        limit_except GET { deny all; }
        include ${TOKEN_SNIPPET};
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header Range \$http_range;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
    }

    location / {
        limit_req zone=pp_page burst=20 nodelay;
        limit_except GET { deny all; }
        include ${TOKEN_SNIPPET};
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
    }
}
EOF
ln -sfn "$SITE_AVAIL" "$SITE_ENABLED"
CHANGES+=("nginx site ${SITE_AVAIL} (+ symlink)")
restore_site() {
    rm -f "$SITE_ENABLED"
    if [ -f "${SITE_AVAIL}.bak-${TS}" ]; then mv "${SITE_AVAIL}.bak-${TS}" "$SITE_AVAIL"; ln -sfn "$SITE_AVAIL" "$SITE_ENABLED"; else rm -f "$SITE_AVAIL"; fi
}
reload_nginx_safely restore_site

# ---------------------------------------------------------------------------------------- settings helper
c_step "7b   Settings helper for the web page (systemd, root-only whitelist)"
c_info "The web page can't touch the host itself. It drops a small request file in data/agent/requests/; this helper"
c_info "(scripts/pp_agent.py) then runs ONLY whitelisted actions: save settings, rotate token, change login, update, self-test."
mkdir -p "${DIR}/data/agent/requests" "${DIR}/data/agent/results"
chown -R 1000:1000 "${DIR}/data/agent" 2>/dev/null || true
cat > /etc/systemd/system/pp-agent.service <<EOF
${MARKER}
[Unit]
Description=Poor Puppy settings helper (processes requests from the web page)

[Service]
Type=oneshot
Environment=POOR_PUPPY_DIR=${DIR}
ExecStart=/usr/bin/python3 ${DIR}/scripts/pp_agent.py --process
TimeoutStartSec=45min
EOF
cat > /etc/systemd/system/pp-agent.path <<EOF
${MARKER}
[Unit]
Description=Poor Puppy settings helper (watches for web page requests)

[Path]
PathExistsGlob=${DIR}/data/agent/requests/*.json
Unit=pp-agent.service

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pp-agent.path >/dev/null 2>&1
CHANGES+=("systemd pp-agent.path/.service (settings helper)")
if python3 "${DIR}/scripts/pp_agent.py" --refresh >/dev/null 2>&1 && systemctl is-active --quiet pp-agent.path; then
    c_ok "Settings helper active (pp-agent.path) and settings snapshot written"
else
    c_warn "Settings helper didn't start cleanly (check: systemctl status pp-agent.path). The rest of the page works."
fi

# ---------------------------------------------------------------------------------------- tests
c_step "8/9  End-to-end tests (through nginx, from this server)"
t_http="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --resolve "${DOMAIN}:80:127.0.0.1" "http://${DOMAIN}/" || true)"
t_noauth="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/" || true)"
[ "$t_http" = "301" ] && c_ok "http:// redirects to https:// (301)" || c_warn "http:// returned ${t_http} (expected 301)"
[ "$t_noauth" = "401" ] && c_ok "https:// without login is refused (401)" || c_warn "https:// without login returned ${t_noauth} (expected 401)"
t_block="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/selftest" || true)"
[ "$t_block" = "403" ] && c_ok "Admin endpoints (/selftest, schedule, mode) are blocked publicly (403)" \
    || c_warn "/selftest returned ${t_block} (expected 403)"
for f in /etc/nginx/sites-enabled/*; do
    n="$(grep -Eo 'server_name[^;]+' "$f" 2>/dev/null | head -1 | awk '{print $2}')"
    [ -n "$n" ] && [ "$n" != "_" ] && [ "$n" != "$DOMAIN" ] || continue
    c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve "${n}:443:127.0.0.1" "https://${n}/" || echo ERR)"
    c_info "Other site still answering: ${n} → HTTP ${c}"
done

# ---------------------------------------------------------------------------------------- renewal
c_step "9/9  Certificate auto-renewal"
systemctl list-timers --all 2>/dev/null | grep -E 'certbot|snap\.certbot' | sed 's/^/      /' || c_warn "No certbot timer listed (check: systemctl list-timers | grep certbot)"
if certbot renew --dry-run --cert-name "$DOMAIN" >/tmp/pp-renew.log 2>&1; then
    c_ok "certbot renew --dry-run for ${DOMAIN} succeeded (auto-renews with your other certs; nginx reloads after)"
else
    c_warn "Renewal dry-run failed (the certificate itself is fine for 90 days). Log: /tmp/pp-renew.log"
fi

trap - ERR
c_step "Done 🎉"
c_info "Open:   https://${DOMAIN}/   (log in with the username/password you set)"
c_info "Local:  127.0.0.1:${PORT} → watchdog (loopback only)    Limits: 30 req/min page, 6/min actions per IP"
c_info "Survives updates: update-pp keeps docker-compose.override.yml (git-ignored) and the nginx site."
c_info "Undo:   sudo ${DIR}/vps_install.sh --uninstall"
