#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

required = ["bs4", "httpx", "PIL", "playwright", "reportlab", "pypdf"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
browser_candidates = [
    os.environ.get("KOLO_CHROMIUM_PATH"),
    "/usr/local/bin/chromium",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
browser = next((value for value in browser_candidates if value and Path(value).exists()), None)
renderer = shutil.which("pdftoppm")
status = "pass" if not missing and browser and renderer else "fail"
print(json.dumps({
    "status": status,
    "missing": missing,
    "chromium": browser,
    "pdftoppm": renderer,
    "paid_calls": 0,
}, sort_keys=True))
raise SystemExit(0 if status == "pass" else 1)
