"""Read-only external performance probe for KPDesk releases."""
from __future__ import annotations

import json
import sys
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


if __name__ == "__main__":
    for resource in ("", "app", "assets/hero/01_hero_main.avif", "fonts/inter/inter-cyrillic-wght-normal.woff2"):
        fetch_headers(resource)
    ok = [google_pagespeed(strategy) for strategy in ("mobile", "desktop")]
    if not all(ok):
        print("NOTICE: Some Google PSI queries were unavailable; no scores are inferred.", flush=True)
        sys.exit(0)
