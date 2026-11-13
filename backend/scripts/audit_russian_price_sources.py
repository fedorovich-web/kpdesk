"""Create-only verification snapshot of approved Russian stores and RUB source prices.

The snapshot contains only auditable source metadata, never products or price quotes.
No foreign-store fallback, FX conversion, training, production writes or Git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from scripts.audit_real_supplier_corpus import ROOT, audit_real_supplier_corpus


def build_russian_price_audit(corpus: Path) -> dict[str, object]:
    sources = audit_real_supplier_corpus(corpus)
    if not sources or any(
        x["seller_country"] != "RU" or x["price_currency"] != "RUB"
        or x["eligible_for_current_price_quotes"] is not False
        for x in sources
    ):
        raise ValueError("Russian-store RUB currency gate not met.")
    original_manifest = corpus / "manifest.json"
    return {
        "schema_version": "russian-store-rub-audit-v1",
        "require_seller_market": "russian_online_store",
        "require_seller_country": "RU",
        "require_native_currency": "RUB",
        "allow_foreign_currency_conversion": False,
        "allow_unverified_live_prices": False,
        "original_manifest_sha256": hashlib.sha256(original_manifest.read_bytes()).hexdigest(),
        "audited_source_count": len(sources),
        "audited_product_candidates": sum(int(x["candidate_product_rows"]) for x in sources),
        "price_freshness_warning": "Five 2023 XLSX originals are historical format samples. "
                                   "2026 SVih list is source-dated but not a live availability quote.",
        "sources": sources,
        "ready_for_real_supervised_training": False,
        "verified_independent_benchmark": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Source-locked Russian-store RUB audit.")
    parser.add_argument("--corpus", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    audit = build_russian_price_audit(args.corpus)
    audit["verified_at_utc"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(audit, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    print(json.dumps({
        "russian_store_sources": audit["audited_source_count"],
        "rub_product_candidates": audit["audited_product_candidates"],
        "approved_foreign_stores": 0,
        "foreign_currency_prices": 0,
        "price_currency": "RUB",
        "current_prices_verified": False,
        "training_approved": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
