#!/usr/bin/env bash
# ============================================================================
# export_ml_bundle.sh — Package SQLite metrics, JSONL exports, and optional
# capture media into a single timestamped tarball for offline ML training.
#
# Safe on a live VPS:
#   - 100% read-only toward the running Docker container (never stops/restarts)
#   - Uses SQLite's online backup API for a consistent snapshot of the WAL DB
#   - Never reads or prints secret keys from .env
#   - Sets chmod 600 on the output archive
#
# Usage:
#   ./scripts/export_ml_bundle.sh                     # default: DB + JSONL + captures (jpg/mp4/wav)
#   ./scripts/export_ml_bundle.sh --days 30           # DB + JSONL + captures newer than 30 days
#   ./scripts/export_ml_bundle.sh --no-media          # metrics only (DB + JSONL + labels, no media)
#   ./scripts/export_ml_bundle.sh --with-review --out /tmp/my.tar.gz
# ============================================================================
set -euo pipefail

WITH_MEDIA=1
WITH_REVIEW=0
WITH_BACKUPS=0
DAYS=0
OUT_PATH=""

usage() {
    cat <<'EOF'
Usage: ./scripts/export_ml_bundle.sh [options]

Default: packs SQLite DB + data/ml_events.jsonl + labels + captured media (*.mp4, *.wav, *.jpg).

Options:
  --with-media     Include captured media (*.mp4, *.wav, *.jpg) from ./captures (DEFAULT)
  --no-media       Metrics only (SQLite DB + JSONL + labels; skip video/audio/image files)
  --days N         When media is included, only pack capture files newer than N days
  --with-review    Also include captures/review_clips/ media files
  --with-backups   Include up to the 3 most recent DB backups from data/backups/
  --out PATH       Custom output .tar.gz path (default: exports/poor-puppy-ml-YYYYMMDD_HHMMSS.tar.gz)
  -h, --help       Show this help message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --with-media)   WITH_MEDIA=1; shift ;;
        --no-media)     WITH_MEDIA=0; shift ;;
        --with-review)  WITH_REVIEW=1; shift ;;
        --with-backups) WITH_BACKUPS=1; shift ;;
        --days)
            [ $# -ge 2 ] || { echo "Error: --days requires a number" >&2; exit 1; }
            DAYS="$2"; shift 2 ;;
        --out)
            [ $# -ge 2 ] || { echo "Error: --out requires a path" >&2; exit 1; }
            OUT_PATH="$2"; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

SCRIPT_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0")"
ROOT="${POOR_PUPPY_DIR:-$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)}"
cd "$ROOT"

STAMP="$(date +%Y%m%d_%H%M%S)"
if [ -z "$OUT_PATH" ]; then
    mkdir -p "$ROOT/exports"
    chmod 700 "$ROOT/exports" 2>/dev/null || true
    OUT_PATH="$ROOT/exports/poor-puppy-ml-${STAMP}.tar.gz"
else
    mkdir -p "$(dirname "$OUT_PATH")"
fi
OUT_ABS="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$OUT_PATH")"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/pp-ml-export.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
BUNDLE_NAME="poor-puppy-ml-${STAMP}"
BUNDLE_DIR="$STAGE/$BUNDLE_NAME"
mkdir -p "$BUNDLE_DIR/data"

GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Python helper does the online SQLite .backup(), stages files, and writes manifest.json
python3 - "$ROOT" "$BUNDLE_DIR" "$WITH_MEDIA" "$WITH_REVIEW" "$WITH_BACKUPS" "$DAYS" "$GIT_BRANCH" "$GIT_COMMIT" <<'PY'
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1]).resolve()
bundle = Path(sys.argv[2]).resolve()
with_media = sys.argv[3] == "1"
with_review = sys.argv[4] == "1"
with_backups = sys.argv[5] == "1"
days = float(sys.argv[6] or 0)
git_branch = sys.argv[7]
git_commit = sys.argv[8]

SAFE_ENV_KEYS = (
    "KEEP_FOOTAGE",
    "CAPTURE_RETENTION_DAYS",
    "KEEP_FAILED_FOOTAGE_DAYS",
    "VIDEO_KEEP_DAYS_OK",
    "CAPTURE_MAX_MB",
    "DB_RETENTION_DAYS",
    "EXPORT_ML_ON_PRUNE",
    "ML_EXPORT_PATH",
    "ALERT_MODE",
    "NO_FOOD_ALERT_HOURS",
    "TING_RATE_MIN",
    "NO_TING_ESCALATE_AFTER",
)


def read_safe_env() -> dict:
    vals = {}
    for fname in (".env.example", ".env"):
        p = root / fname
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=(.*)$", line)
            if m and m.group(1) in SAFE_ENV_KEYS:
                vals[m.group(1)] = re.sub(r"\s+#.*$", "", m.group(2)).strip()
    return vals


env_cfg = read_safe_env()

import subprocess

# 1. Consistent SQLite backup:
#    Prefer running SQLite's online backup API inside the running container as uid=1000 (shares WAL/SHM locks
#    with the live daemon and never creates root-owned files). If the container is not running, fall back to
#    staging db + -wal (NEVER -shm), running .backup(), and verifying PRAGMA integrity_check == 'ok' with retries.
db_candidates = [root / "data" / "kibble_history.db", root / "data" / "feeder_watchdog.db", root / "kibble_history.db"]
src_db = next((p for p in db_candidates if p.is_file()), None)
db_counts = {"total": 0, "success": 0, "clog": 0, "with_media": 0, "media_purged": 0, "ml_exported": 0}

if src_db is not None:
    dst_db = bundle / "data" / "kibble_history.db"
    backed_up = False

    # Path A: Container-side online backup as uid=1000 when poor-puppy-watchdog is running for this repo
    if shutil.which("docker") and src_db.parent == (root / "data"):
        try:
            insp = subprocess.run(
                [
                    "docker", "inspect", "-f",
                    "{{.State.Running}}|{{range .Mounts}}{{if eq .Destination \"/app/data\"}}{{.Source}}{{end}}{{end}}",
                    "poor-puppy-watchdog",
                ],
                capture_output=True, text=True, timeout=5,
            )
            parts = insp.stdout.strip().split("|", 1) if insp.returncode == 0 else []
            if len(parts) == 2 and parts[0] == "true" and Path(parts[1]).resolve() == (root / "data").resolve():
                tmp_name = f".export_snap_{os.getpid()}.db"
                container_src = f"/app/data/{src_db.name}"
                container_dst = f"/app/data/{tmp_name}"
                host_tmp = root / "data" / tmp_name
                try:
                    r_exec = subprocess.run(
                        [
                            "docker", "compose", "exec", "-T", "feeder-watchdog", "python", "-c",
                            "import sqlite3,sys;"
                            "s=sqlite3.connect(sys.argv[1],timeout=30.0);"
                            "s.execute('VACUUM INTO ?', (sys.argv[2],));s.close();"
                            "d=sqlite3.connect(sys.argv[2]);"
                            "chk=d.execute('PRAGMA integrity_check').fetchall();d.close();"
                            "sys.exit(0 if chk==[('ok',)] else 1)",
                            container_src, container_dst,
                        ],
                        cwd=root, capture_output=True, text=True, timeout=45,
                    )
                    if r_exec.returncode == 0 and host_tmp.is_file():
                        shutil.move(str(host_tmp), str(dst_db))
                        backed_up = True
                finally:
                    for p in (host_tmp, Path(str(host_tmp) + "-wal"), Path(str(host_tmp) + "-shm")):
                        if p.exists():
                            try:
                                p.unlink()
                            except OSError:
                                pass
        except Exception:
            pass

    # Path B: Host fallback — stage db + -wal (never -shm), VACUUM INTO standalone DB, and enforce PRAGMA integrity_check
    if not backed_up:
        raw_db_dir = bundle.parent / "raw_db"
        raw_db_dir.mkdir(parents=True, exist_ok=True)
        last_err = "unknown integrity failure"
        for attempt in range(3):
            for old in raw_db_dir.iterdir():
                old.unlink()
            if dst_db.exists():
                dst_db.unlink()
            staged_src = raw_db_dir / src_db.name
            try:
                shutil.copyfile(src_db, staged_src)
                wal_sidecar = Path(str(src_db) + "-wal")
                if wal_sidecar.is_file():
                    try:
                        shutil.copyfile(wal_sidecar, Path(str(staged_src) + "-wal"))
                    except FileNotFoundError:
                        pass
                src_conn = sqlite3.connect(str(staged_src), timeout=30.0)
                try:
                    src_conn.execute("VACUUM INTO ?", (str(dst_db),))
                finally:
                    src_conn.close()
                dst_conn = sqlite3.connect(str(dst_db))
                try:
                    chk = dst_conn.execute("PRAGMA integrity_check").fetchall()
                    if chk != [("ok",)]:
                        raise sqlite3.DatabaseError(f"PRAGMA integrity_check failed: {chk}")
                    tables = {
                        r[0]
                        for r in dst_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    }
                    if "feed_events" not in tables or dst_db.stat().st_size < 4096:
                        raise sqlite3.DatabaseError("Exported DB is missing feed_events table or truncated")
                    dst_conn.execute("SELECT COUNT(*) FROM feed_events").fetchone()
                    backed_up = True
                finally:
                    dst_conn.close()
                if backed_up:
                    break
            except (sqlite3.Error, OSError) as exc:
                last_err = str(exc)
                if attempt < 2:
                    time.sleep(0.15)
        if not backed_up:
            if dst_db.exists():
                dst_db.unlink()
            print(f"✗ Error: SQLite export failed integrity check after 3 attempts: {last_err}", file=sys.stderr)
            sys.exit(1)

    dst_conn = sqlite3.connect(str(dst_db))
    try:
        cur = dst_conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(feed_events)").fetchall()}
        db_counts["total"] = cur.execute("SELECT COUNT(*) FROM feed_events").fetchone()[0]
        db_counts["success"] = cur.execute("SELECT COUNT(*) FROM feed_events WHERE success = 1").fetchone()[0]
        db_counts["clog"] = cur.execute("SELECT COUNT(*) FROM feed_events WHERE clog_detected = 1").fetchone()[0]
        db_counts["with_media"] = cur.execute(
            "SELECT COUNT(*) FROM feed_events WHERE COALESCE(video_clip, audio_clip, before_image, after_image, annotated_image) IS NOT NULL"
        ).fetchone()[0]
        if "media_purged_at" in cols:
            db_counts["media_purged"] = cur.execute(
                "SELECT COUNT(*) FROM feed_events WHERE media_purged_at IS NOT NULL"
            ).fetchone()[0]
        if "ml_exported_at" in cols:
            db_counts["ml_exported"] = cur.execute(
                "SELECT COUNT(*) FROM feed_events WHERE ml_exported_at IS NOT NULL"
            ).fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        dst_conn.close()

# 2. JSONL export & learned audio model & ground-truth labels
ml_rel = env_cfg.get("ML_EXPORT_PATH") or "data/ml_events.jsonl"
ml_src = Path(ml_rel) if Path(ml_rel).is_absolute() else (root / ml_rel)
jsonl_lines = 0
if ml_src.is_file():
    dst_jsonl = bundle / "data" / "ml_events.jsonl"
    shutil.copy2(ml_src, dst_jsonl)
    with open(dst_jsonl, "r", encoding="utf-8", errors="ignore") as fh:
        jsonl_lines = sum(1 for line in fh if line.strip())

model_src = root / "data" / "audio_model.json"
if model_src.is_file():
    shutil.copy2(model_src, bundle / "data" / "audio_model.json")

labels_src = root / "captures" / "review_clips" / "training_labels.json"
if labels_src.is_file():
    (bundle / "captures" / "review_clips").mkdir(parents=True, exist_ok=True)
    shutil.copy2(labels_src, bundle / "captures" / "review_clips" / "training_labels.json")

# 3. Optional recent DB backups (up to 3 latest)
if with_backups and (root / "data" / "backups").is_dir():
    backups = sorted((root / "data" / "backups").glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    if backups:
        (bundle / "data" / "backups").mkdir(parents=True, exist_ok=True)
        for b in backups:
            shutil.copy2(b, bundle / "data" / "backups" / b.name)

# 4. Optional capture media
now = time.time()
cutoff = (now - days * 86400.0) if days > 0 else 0.0
MEDIA_EXTS = {".mp4", ".wav", ".jpg", ".jpeg"}

if with_media and (root / "captures").is_dir():
    (bundle / "captures").mkdir(parents=True, exist_ok=True)
    for f in sorted((root / "captures").iterdir()):
        try:
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                if cutoff <= 0 or f.stat().st_mtime >= cutoff:
                    shutil.copy2(f, bundle / "captures" / f.name)
        except FileNotFoundError:
            continue

if with_review and (root / "captures" / "review_clips").is_dir():
    (bundle / "captures" / "review_clips").mkdir(parents=True, exist_ok=True)
    for f in sorted((root / "captures" / "review_clips").iterdir()):
        try:
            if f.is_file() and (f.suffix.lower() in MEDIA_EXTS or f.name == "training_labels.json"):
                if cutoff <= 0 or f.stat().st_mtime >= cutoff or f.name == "training_labels.json":
                    shutil.copy2(f, bundle / "captures" / "review_clips" / f.name)
        except FileNotFoundError:
            continue

# 5. Build manifest.json
files_manifest = {}
for p in sorted(bundle.rglob("*")):
    if p.is_file():
        files_manifest[str(p.relative_to(bundle))] = p.stat().st_size

manifest = {
    "exported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    "git": {"branch": git_branch, "commit": git_commit},
    "retention_config": env_cfg,
    "feed_events": db_counts,
    "ml_events_jsonl_rows": jsonl_lines,
    "options": {
        "with_media": with_media,
        "with_review": with_review,
        "with_backups": with_backups,
        "days": days if days > 0 else None,
    },
    "files": files_manifest,
}
(bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

umask 077
COPYFILE_DISABLE=1 tar -C "$STAGE" -czf "$OUT_ABS" "$BUNDLE_NAME"
chmod 600 "$OUT_ABS"

SIZE_BYTES="$(wc -c < "$OUT_ABS" | tr -d ' ')"
SIZE_HUMAN="$(du -h "$OUT_ABS" | awk '{print $1}')"
HOST_HINT="${USER:-ubuntu}@$(hostname -I 2>/dev/null | awk '{print $1}' || hostname)"
HOST_HINT="$(echo "$HOST_HINT" | tr -d ' ')"
[ "$HOST_HINT" = "${USER:-ubuntu}@" ] && HOST_HINT="${USER:-ubuntu}@<vps-ip>"

echo "✅ ML bundle created:"
echo "   Archive:  $OUT_ABS"
echo "   Size:     $SIZE_HUMAN (${SIZE_BYTES} bytes, mode 0600)"
python3 - "$BUNDLE_DIR/manifest.json" <<'PY'
import json, sys
m = json.loads(open(sys.argv[1]).read())
fe = m["feed_events"]
print(f"   DB rows:  {fe['total']} feed_events (ok={fe['success']}, clogs={fe['clog']}, with_media={fe['with_media']}, purged={fe['media_purged']})")
print(f"   JSONL:    {m['ml_events_jsonl_rows']} exported rows  |  Files packed: {len(m['files'])}")
PY
echo ""
echo "📥 Copy off the VPS from your laptop/Mac:"
echo "   scp ${HOST_HINT}:${OUT_ABS} ."
