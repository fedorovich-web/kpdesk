"""Read-only external performance probe for KPDesk releases."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://kpdesk.ru/"
AUDIT_IDS = (
    "first-contentful-paint", "largest-contentful-paint", "speed-index",
    "total-blocking-time", "cumulative-layout-shift", "server-response-time",
    "uses-long-cache-ttl", "render-blocking-resources", "unused-javascript",
    "unused-css-rules", "uses-responsive-images", "modern-image-formats",
    "uses-optimized-images", "lcp-discovery-insight", "font-display-insight",
    "cache-insight", "network-dependency-tree-insight",
)


def fetch_headers(path: str) -> None:
    req = Request(BASE + path, method="HEAD",
                  headers={"User-Agent": "KPDesk-performance-audit/1.0"})
    try:
        with urlopen(req, timeout=25) as response:
            keys = ("cache-control", "content-encoding", "content-type",
                    "content-length", "etag", "vary")
            print(json.dumps({"path": path, "status": response.status,
                              "headers": {key: response.headers.get(key) for key in keys}},
                             ensure_ascii=False), flush=True)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"HEAD {path}: {type(exc).__name__}: {exc}", flush=True)


def google_pagespeed(strategy: str) -> bool:
    params = [("url", BASE), ("strategy", strategy), ("locale", "en")]
    params.extend(("category", x) for x in (
        "performance", "accessibility", "best-practices", "seo"
    ))
    url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "KPDesk-performance-audit/1.0"})
    try:
        with urlopen(req, timeout=150) as response:
            data = json.load(response)
    except HTTPError as exc:
        body = exc.read(1000).decode("utf-8", errors="replace")
        print(f"Google PSI {strategy} HTTP {exc.code}: {body[:500]}", flush=True)
        return False
    except (URLError, TimeoutError, ValueError) as exc:
        print(f"Google PSI {strategy}: {type(exc).__name__}: {exc}", flush=True)
        return False

    lighthouse = data.get("lighthouseResult", {})
    scores = {key: (None if not item.get("score") and item.get("score") != 0
                    else round(item["score"] * 100))
              for key, item in lighthouse.get("categories", {}).items()}
    audits = lighthouse.get("audits", {})
    findings = []
    for key in AUDIT_IDS:
        item = audits.get(key)
        if item:
            findings.append({
                "id": key, "score": item.get("score"),
                "display": item.get("displayValue"),
                "details": item.get("details", {}).get("overallSavingsMs"),
            })
    result = {
        "strategy": strategy, "timestamp": data.get("analysisUTCTimestamp"),
        "lighthouseVersion": lighthouse.get("lighthouseVersion"),
        "scores": scores, "findings": findings,
        "runtimeError": lighthouse.get("runtimeError"),
    }
    print("Google PageSpeed Insights " + json.dumps(result, ensure_ascii=False), flush=True)
    with open(f"pagespeed-{strategy}.json", "w", encoding="utf-8") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    return True


def _expected_frontend_assets() -> tuple[str, str]:
    """Use this commit's actual Vite build as the post-deploy version marker."""
    root = Path(__file__).resolve().parents[2]
    local_html = (root / "frontend" / "dist" / "index.html").read_text(encoding="utf-8")
    js = re.search(r'/assets/(index-[A-Za-z0-9_-]+\.js)', local_html)
    css = re.search(r'/assets/(index-[A-Za-z0-9_-]+\.css)', local_html)
    if js is None or css is None:
        raise ValueError("Built index.html does not contain a versioned JS and CSS entry")
    return js.group(1), css.group(1)


def wait_for_production_release() -> bool:
    """Measure only after production HTML references this commit's build."""
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        return True
    try:
        js, css = _expected_frontend_assets()
    except (OSError, ValueError) as exc:
        print(f"Missing expected build fingerprint: {exc}", flush=True)
        return False
    print(f"Waiting for live frontend assets: {js}, {css}", flush=True)
    for attempt in range(32):
        try:
            with urlopen(Request(BASE, headers={"Cache-Control": "no-cache"}),
                         timeout=12) as response:
                html = response.read(100000).decode("utf-8", errors="replace")
                if f"/assets/{js}" in html and f"/assets/{css}" in html:
                    for resource in (js, css):
                        with urlopen(Request(BASE + "assets/" + resource, method="HEAD"),
                                     timeout=12) as asset:
                            if asset.status != 200:
                                raise HTTPError(BASE + resource, asset.status,
                                                "Unexpected asset response", asset.headers, None)
                    print(f"Detected exact deployed frontend build after {attempt} checks.", flush=True)
                    return True
                print(f"Live frontend not deployed yet: expected {js} and {css}", flush=True)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"Live release not ready yet: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(15)
    print("Deployment not observed: cannot claim a post-release audit.", flush=True)
    return False

if __name__ == "__main__":
    if not wait_for_production_release():
        sys.exit(2)
    for resource in ("", "app", "assets/hero/01_hero_main.avif", "assets/hero/01_hero_main-480w.avif", "fonts/inter/inter-cyrillic-wght-normal.woff2"):
        fetch_headers(resource)
    ok = [google_pagespeed(strategy) for strategy in ("mobile", "desktop")]
    if not all(ok):
        print("NOTICE: Some Google PSI queries were unavailable; no scores are inferred.", flush=True)
        sys.exit(0)
