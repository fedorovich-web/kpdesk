"""Alternative natural-expression synthetic corpus; original A39 data unchanged.

This is a newly authored training transformation with no real customer text.
Scores on re-used SKU intents are not independent production evidence.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kpdesk.matching.cli import load_dataset
from scripts.build_category_curriculum import _ACCESSORIES, build_category_curriculum
from scripts.build_synthetic_ml_curriculum import _SPECS

_PHRASES = {
    **{f"intent-{sku}": phrase for _, sku, _, phrase, _, _ in _SPECS},
    **{f"intent-{sku}": phrase for _, sku, _, phrase in _ACCESSORIES},
}
_FORMS = (
    "{item}",
    "Нужен {item}, три штуки",
    "Подобрать {item} для снабжения",
)


def build_natural_category_curriculum() -> dict[str, Any]:
    document = build_category_curriculum()
    queries = document["queries"]
    if not isinstance(queries, list):
        raise TypeError("Curated source corpus must contain request examples.")
    for query in queries:
        group = query["query_group_id"]
        phrase = _PHRASES.get(group)
        if phrase is None:
            raise ValueError("Missing independently authored product wording.")
        number = int(query["query_id"].rsplit("-", 1)[-1])
        if not 1 <= number <= 3:
            raise ValueError("Unexpected original query variant.")
        query["request"]["description"] = _FORMS[number - 1].format(item=phrase)
    document["provenance"]["annotation_method"] = (
        "local_hand_curated_natural_wording_category_examples_v2"
    )
    document["provenance"]["source_customer_gold"] = False
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create natural synthetic category wordings.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = build_natural_category_curriculum()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    data = load_dataset(args.output)
    print(json.dumps({
        "source_sha256": data.source_sha256,
        "groups": len({row.query_group_id for row in data.queries}),
        "queries": len(data.queries),
        "categories": len({row.request.category for row in data.queries}),
        "customer_gold": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
