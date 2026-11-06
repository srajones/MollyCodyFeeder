#!/usr/bin/env python3
"""Review Lab entry point (LAN testing tool): `python review_server.py [port]`.

Implementation: watchdog/review/ (manager.py = clips & labels, server.py = HTTP) and the page template
watchdog/web/templates/review.html.
"""
import logging
import sys

from watchdog.review.manager import CLIPS_DIR, LABELS_JSON, ReviewManager  # noqa: F401  (re-export)
from watchdog.review.server import HTML_PAGE, get_lan_ip, run_server  # noqa: F401  (re-export)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

if __name__ == "__main__":
    run_server(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
