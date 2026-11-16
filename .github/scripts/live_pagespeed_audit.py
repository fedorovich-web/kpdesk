"""Read-only external performance probe for KPDesk releases."""
from __future__ import annotations

import json
import os
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


def wait_for_production_release() -> bool:
    """Avoid presenting pre-deploy data as a post-merge regression result."""
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        return True
    for attempt in range(32):
        try:
            # This release ships a new, small variant absent from the previous
            # deployment. Do not mistake an old cached image for a fresh build.
            resource = "assets/hero/01_hero_main-480w.avif"
            request = Request(BASE + resource, method="HEAD")
            with urlopen(request, timeout=12) as response:
                cache = response.headers.get("Cache-Control", "")
                size = response.headers.get("Content-Length", "")
                if (response.status == 200 and response.headers.get("Content-Type") == "image/avif"
                        and size == "20006" and "max-age=2592000" in cache):
                    with urlopen(Request(BASE, headers={"Cache-Control": "no-cache"}),
                                 timeout=12) as home:
                        html = home.read(100000).decode("utf-8", errors="replace")
                        if "01_hero_main-480w.avif 480w" in html:
                            print(f"Detected deployed responsive-AVIF release after {attempt} checks.", flush=True)
                            return True
                print(f"Live release not ready yet: {response.status} size={size!r} cache={cache!r}", flush=True)
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
