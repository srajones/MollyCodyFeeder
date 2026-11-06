"""Review Lab HTTP server (LAN, optional REVIEW_TOKEN). The page lives in watchdog/web/templates/review.html."""

import hmac
import json
import logging
import mimetypes
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from watchdog.review.manager import CLIPS_DIR, ReviewManager

logger = logging.getLogger("poor_puppy.review_server")
HTML_PAGE = (Path(__file__).resolve().parent.parent / "web" / "templates" / "review.html").read_text()


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_server(port: int = 8765) -> None:
    mgr = ReviewManager()

    review_token = os.getenv("REVIEW_TOKEN", "").strip()

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            """Optional REVIEW_TOKEN: open the page once as /?t=<token> (sets a cookie)."""
            if not review_token:
                return True
            q = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            cookie = ""
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "rt":
                    cookie = v
            return any(hmac.compare_digest(c.encode(), review_token.encode()) for c in (q, cookie) if c)

        def _deny(self) -> None:
            self._send_json(401, {"error": "Open the page as /?t=<REVIEW_TOKEN> first"})

        def _send_json(self, code: int, obj: Any) -> None:
            data = json.dumps(obj, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if not self._authorized():
                self._deny()
                return
            if parsed.path == "/" or parsed.path == "/index.html":
                body = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if review_token:
                    self.send_header("Set-Cookie", f"rt={review_token}; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/clips":
                self._send_json(200, {"clips": mgr.list_clips()})
                return
            if parsed.path == "/api/status":
                self._send_json(200, {"busy_status": mgr.busy_status})
                return
            if parsed.path.startswith("/clips/"):
                fname = Path(parsed.path.replace("/clips/", "")).name
                fpath = CLIPS_DIR / fname
                if not fpath.exists():
                    self.send_error(404, "File not found")
                    return
                ctype, _ = mimetypes.guess_type(str(fpath))
                ctype = ctype or "application/octet-stream"
                file_size = fpath.stat().st_size
                range_hdr = self.headers.get("Range")
                if range_hdr and range_hdr.startswith("bytes="):
                    try:
                        r_spec = range_hdr.split("=", 1)[1].split(",")[0].strip()
                        s_str, e_str = r_spec.split("-", 1)
                        start = int(s_str) if s_str else 0
                        end = int(e_str) if e_str else (file_size - 1)
                        end = min(end, file_size - 1)
                        if start <= end and start < file_size:
                            length = end - start + 1
                            with open(fpath, "rb") as f:
                                f.seek(start)
                                chunk = f.read(length)
                            self.send_response(206)
                            self.send_header("Content-Type", ctype)
                            self.send_header("Accept-Ranges", "bytes")
                            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                            self.send_header("Content-Length", str(len(chunk)))
                            self.send_header("Cache-Control", "no-cache")
                            self.end_headers()
                            self.wfile.write(chunk)
                            return
                    except Exception:
                        pass
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404, "Not found")

        def do_POST(self):
            if not self._authorized():
                self._deny()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 64 * 1024:
                self._send_json(413, {"error": "Body too large"})
                return
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(raw) if raw.strip() else {}

            if self.path == "/api/label":
                clip_id = body.get("clip_id", "")
                existing = mgr.get_clip(clip_id)
                if not existing:
                    self._send_json(404, {"error": "Unknown clip_id"})
                    return
                for k in (
                    "user_reviewed",
                    "feed_success",
                    "is_clog",
                    "heard_motor",
                    "heard_kibble",
                    "saw_kibble_accumulate",
                    "bowl_had_food_before",
                    "dog_at_bowl",
                    "notes",
                ):
                    if k in body:
                        existing[k] = body[k]
                mgr._save_clip_record(existing)
                logger.info("Saved user ground-truth label for %s: %s", clip_id, body)
                self._send_json(200, {"ok": True, "clip": existing})
                return

            if self.path == "/api/capture":
                portions = max(0, min(20, int(body.get("portions", 0))))
                duration = max(3.0, min(120.0, float(body.get("duration", 11.0))))
                rec = mgr.trigger_new_capture(dispense_portions=portions, duration_sec=duration)
                self._send_json(200, {"ok": True, "clip": rec})
                return

            if self.path == "/api/say":
                text = str(body.get("text", "")).strip()
                with mgr.lock:
                    mgr.busy_status = f"Streaming Speaker TTS over Cloud: '{text[:40]}'..."
                try:
                    volume = max(0.5, min(4.0, float(body.get("volume") or mgr.cfg.tts_volume)))
                    res = mgr.client.speak_tts(text=text, volume=volume, prepend_chime=False)
                finally:
                    with mgr.lock:
                        mgr.busy_status = ""
                logger.info("Speaker TTS result: %s", res)
                self._send_json(200, res)
                return

            self.send_error(404, "Not found")

        def log_message(self, fmt, *args):
            pass

    lan_ip = get_lan_ip()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logger.info("==================================================================")
    logger.info("Review Lab Server listening on:")
    logger.info("  Local Mac:  http://localhost:%d", port)
    logger.info("  LAN / Phone: http://%s:%d", lan_ip, port)
    if not review_token:
        logger.warning(
            "  ⚠️  REVIEW_TOKEN is not set — listening on 0.0.0.0:%d without auth (set REVIEW_TOKEN in .env on shared/VPS hosts)",
            port,
        )
    logger.info("==================================================================")
    srv.serve_forever()
