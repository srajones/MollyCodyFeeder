"""Storage paths for the standalone Molly & Cody Smart Feeder core package."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(os.getenv("POOR_PUPPY_DIR", str(PACKAGE_ROOT.parent.parent)))
DATA_DIR = Path(os.getenv("POOR_PUPPY_DATA_DIR", str(PROJECT_ROOT / "data")))
