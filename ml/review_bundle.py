#!/usr/bin/env python3
"""Offline ML-bundle review tool (v0.6): step through every exported feed event and record ground truth.

Usage:
    python3 ml/review_bundle.py [BUNDLE] [--port 8777]

BUNDLE is an `exports/poor-puppy-ml-*.tar.gz` archive or an already-extracted bundle directory.
If omitted, the newest bundle under `exports/` is used. Archives are extracted next to themselves
(inside the git-ignored `exports/` folder) the first time.

* Standard library only (no numpy/cv2 needed on the laptop), binds to 127.0.0.1 only.
* Never touches the feeder or the cloud; the bundle DB is opened read-only.
* Ground-truth labels are written to `ml/labels/feed_event_labels.json` (small, meant to be committed),
  keyed by "<event timestamp>|<event id>" so they survive re-exports of the same VPS database.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
import sys
import tarfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "exports"
HTML_FILE = Path(__file__).resolve().parent / "review_bundle.html"
LABELS_FILE = Path(__file__).resolve().parent / "labels" / "feed_event_labels.json"
MEDIA_EXTS = {".mp4", ".wav", ".jpg", ".jpeg", ".png"}
MEDIA_FIELDS = ("video_clip", "audio_clip", "before_image", "after_image", "annotated_image")

_labels_lock = threading.Lock()


# ---------------------------------------------------------------- bundle discovery / extraction
def _safe_extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")  # rejects absolute paths / .. / links outside dest
        except TypeError:  # very old Python without extraction filters
            for m in tf.getmembers():
                target = (dest / m.name).resolve()
                if not str(target).startswith(str(dest.resolve())) or m.issym() or m.islnk():
                    raise RuntimeError(f"Unsafe path in archive: {m.name}")
            tf.extractall(dest)


def resolve_bundle(arg: Optional[str]) -> Path:
    if arg:
        p = Path(arg).expanduser().resolve()
    else:
        cands = sorted(EXPORTS.glob("poor-puppy-ml-*.tar.gz")) + sorted(
            d for d in EXPORTS.glob("poor-puppy-ml-*") if d.is_dir()
        )
        if not cands:
            sys.exit(f"No bundle found in {EXPORTS}. Pass the .tar.gz or extracted directory.")
        p = max(cands, key=lambda c: c.stat().st_mtime)
    if p.is_file() and p.name.endswith(".tar.gz"):
        out_dir = p.parent / p.name[: -len(".tar.gz")]
        if not out_dir.is_dir():
            print(f"Extracting {p.name} -> {out_dir.parent} ...", flush=True)
            _safe_extract(p, p.parent)
        p = out_dir
    if not p.is_dir():
        sys.exit(f"Bundle directory not found: {p}")
    # The archive holds a single top-level folder; accept either that folder or its parent.
    if not (p / "data").is_dir():
        inner = [d for d in p.iterdir() if d.is_dir() and (d / "data").is_dir()]
        if len(inner) == 1:
            p = inner[0]
    return p


def find_db(bundle: Path) -> Path:
    dbs = [d for d in (bundle / "data").glob("*.db")]
    if not dbs:
        dbs = [d for d in bundle.rglob("*.db") if "backups" not in d.parts]
    if not dbs:
        sys.exit(f"No SQLite DB in {bundle}/data")
    return max(dbs, key=lambda d: d.stat().st_size)


# ---------------------------------------------------------------- data access
class Bundle:
    def __init__(self, path: Path):
        self.path = path
        self.db_path = find_db(path)
        # basename -> path relative to the bundle (VPS absolute paths in the DB won't exist locally)
        self.media_index: Dict[str, str] = {}
        for f in path.rglob("*"):
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                rel = str(f.relative_to(path))
                # Prefer top-level captures/ over review_clips/ duplicates
                if f.name not in self.media_index or "review_clips" in self.media_index[f.name]:
                    self.media_index[f.name] = rel

    def _media_url(self, stored: Optional[str]) -> Optional[str]:
        if not stored:
            return None
        rel = self.media_index.get(Path(stored).name)
        return f"/media/{quote(rel)}" if rel else None

    def events(self) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM feed_events ORDER BY timestamp ASC, id ASC").fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            row = dict(r)
            try:
                details = json.loads(row.pop("details_json") or "{}")
            except (TypeError, ValueError):
                details = {}
            row["details"] = details
            row["media"] = {k: self._media_url(row.get(k) or details.get(k)) for k in MEDIA_FIELDS}
            row["media_names"] = {k: (Path(v).name if (v := row.get(k) or details.get(k)) else None) for k in MEDIA_FIELDS}
            out.append(row)
        return out


# ---------------------------------------------------------------- labels
def load_labels() -> Dict[str, Any]:
    if LABELS_FILE.is_file():
        try:
            return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            backup = LABELS_FILE.with_suffix(f".corrupt-{datetime.now():%Y%m%d_%H%M%S}.json")
            LABELS_FILE.rename(backup)
            print(f"WARNING: labels file was corrupt; moved to {backup}", flush=True)
    return {}


def save_label(key: str, label: Dict[str, Any]) -> Dict[str, Any]:
    with _labels_lock:
        labels = load_labels()
        label = dict(label)
        label["labeled_at"] = datetime.now().isoformat(timespec="seconds")
        labels[key] = label
        LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LABELS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(labels.items())), indent=2) + "\n", encoding="utf-8")
        tmp.replace(LABELS_FILE)  # atomic: a crash never leaves a half-written labels file
        return label


# ---------------------------------------------------------------- HTTP
def make_handler(bundle: Bundle):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PoorPuppyReview/0.6"

        def log_message(self, fmt, *args):  # quiet: only log errors
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/events":
                self._json(200, {
                    "bundle": bundle.path.name,
                    "db": bundle.db_path.name,
                    "events": bundle.events(),
                    "labels": load_labels(),
                })
                return
            if path.startswith("/media/"):
                self._serve_media(unquote(path[len("/media/"):]))
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/label":
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
                key = str(payload["key"])
                label = payload["label"]
                if not isinstance(label, dict):
                    raise ValueError("label must be an object")
            except (KeyError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True, "label": save_label(key, label)})

        def _serve_media(self, rel: str) -> None:
            target = (bundle.path / rel).resolve()
            if not str(target).startswith(str(bundle.path.resolve()) + "/") or not target.is_file():
                self._json(404, {"error": "no such media"})
                return
            size = target.stat().st_size
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            length = end - start + 1
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with target.open("rb") as fh:
                fh.seek(start)
                remaining = length
                try:
                    while remaining > 0:
                        chunk = fh.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # browser aborted (normal when seeking video)

    return Handler


def main() -> None:
    global LABELS_FILE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", nargs="?", help="exports/poor-puppy-ml-*.tar.gz or extracted directory")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--labels", help="labels JSON path (default: ml/labels/feed_event_labels.json)")
    args = ap.parse_args()
    if args.labels:
        LABELS_FILE = Path(args.labels).expanduser().resolve()

    bundle = Bundle(resolve_bundle(args.bundle))
    n = len(bundle.events())
    print(f"Bundle: {bundle.path}\nDB:     {bundle.db_path.name} ({n} feed_events, {len(bundle.media_index)} media files)")
    print(f"Labels: {LABELS_FILE}")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(bundle))
    print(f"Open:   http://127.0.0.1:{args.port}/   (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
