"""Every API URL the pages call must exist (a removed route broke every property page once).

Extracts `/api/...` paths from the templates and app.js, turns `${...}` / `{{ ... }}` pieces into a
wildcard segment, and checks each against the app's registered routes and HTTP methods.
Run: .venv/bin/python tests/test_ui_api_routes.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/ui_api_routes_test.db")

from app.main import app  # noqa: E402

# API.get("/api/..."), fetch(`/api/...`), API.post(`/api/...${x}...`) … and plain href / src attributes.
CALL_RE = re.compile(r"""(?:API\.(get|post|patch|put|delete|upload)|fetch)\(\s*[`'"](/api/[^`'"]*)[`'"]""")
ATTR_RE = re.compile(r"""(?:href|src)=["'`](/api/[^"'`]*)["'`]""")


def _norm(path: str) -> str:
    path = path.split("?")[0]
    if "${" in path and "}" not in path.split("${")[-1]:
        path = path.split("${")[0]                     # expression cut off at a nested quote (query-string part)
    path = re.sub(r"\$\{[^}]*\}", "§", path)           # JS template expressions
    path = re.sub(r"\{\{[^}]*\}\}", "§", path)          # Jinja expressions
    path = re.sub(r"'\s*\+[^+]*\+\s*'", "§", path)      # 'a/' + id + '/b'
    return path.rstrip("/") or "/"


def _route_patterns() -> list[tuple[re.Pattern, set[str]]]:
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api/"):
            continue
        rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$"
        out.append((re.compile(rx), set(getattr(r, "methods", None) or {"GET"})))
    return out


def main() -> int:
    routes = _route_patterns()
    files = glob.glob("app/templates/*.html") + ["app/static/js/app.js"]
    problems, checked = [], 0
    for f in files:
        text = open(f).read()
        calls = [(m.group(1) or "get", m.group(2)) for m in CALL_RE.finditer(text)]
        calls += [("get", m.group(1)) for m in ATTR_RE.finditer(text)]
        for method, raw in calls:
            path = _norm(raw)
            if "§" in path.split("/")[2:3]:   # dynamic prefix — can't check
                continue
            method = "POST" if method == "upload" else method.upper()
            if method == "GET" and raw.startswith("/api/") and "fetch(" in raw:
                method = "GET"
            concrete = path.replace("§", "x")
            hits = [ms for rx, ms in routes if rx.match(concrete)]
            checked += 1
            if not hits:
                problems.append(f"{f}: {method} {raw} — no such route")
            elif method != "GET" and not any(method in ms for ms in hits):
                # fetch() defaults to GET; only flag explicit API.<method> mismatches
                if method in ("POST", "PATCH", "PUT", "DELETE"):
                    problems.append(f"{f}: {method} {raw} — route exists but not for {method}")
    for p in problems:
        print("  ✗", p)
    print(f"checked {checked} UI → API calls in {len(files)} files")
    if problems:
        print(f"  ❌ {len(problems)} broken")
        return 1
    print("  ✅ ALL UI API CALLS RESOLVE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
