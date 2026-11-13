"""Immutable-source quality triage for ambiguous supplier identifiers and prices.

This is not the matching/ML labeling workflow. Generated cases require human
review and cannot authorize catalog import, training, or publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from openpyxl import load_workbook

from scripts.audit_recent_supplier_candidates import (
    EXPECTED,
    ROOT,
    audit_recent_supplier_candidates,
)

MAX_CASES_PER_ISSUE = 20
MAX_PACKET_BYTES = 1_000_000
SOURCE_IDS = (
    "avt_el_ecoplast_20260615",
    "shveigid_20261021",
    "etk_rezerv_20260316",
    "technoas_20260916",
)


def _positive_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    amount = Decimal(str(value))
    return amount.is_finite() and amount > 0


def _case(
    source_id: str,
    sheet: str,
    rows: list[int],
    issue: str,
    name: str,
    article: str | None,
    *,
    internal_id: str | None = None,
    observed: dict[str, str] | None = None,
) -> dict[str, object]:
    if not rows or any(row < 1 for row in rows):
        raise ValueError("Quality case requires original positive row coordinates.")
    locator = f"{source_id}|{sheet}|{issue}|{','.join(map(str, rows))}"
    return {
        "case_id": "quality-" + hashlib.sha256(locator.encode()).hexdigest()[:20],
        "source_id": source_id,
        "original_sha256": EXPECTED[source_id][3],
        "issue": issue,
        "sheet": sheet,
        "source_rows": rows,
        "name_excerpt": name[:160],
        "source_article_raw": article,
        "source_item_id_raw": internal_id,
        "observed": observed or {},
    }


def _even_sample(items: list[dict[str, object]], count: int) -> list[dict[str, object]]:
    if len(items) <= count:
        return items
    return [items[i * len(items) // count] for i in range(count)]


def _ecoplast(root: Path, limit: int) -> tuple[dict[str, int], list[dict[str, object]]]:
    book = load_workbook(root / EXPECTED["avt_el_ecoplast_20260615"][2],
                         read_only=True, data_only=True, keep_links=False)
    try:
        sheet = book.worksheets[0]
        duplicate_candidates: dict[str, list[tuple[int, str, str, str]]] = defaultdict(list)
        invalid_packs: list[dict[str, object]] = []
        for row_num, (sku, name, unit, pack, _, _) in enumerate(
            sheet.iter_rows(min_row=3, max_col=6, values_only=True), start=3,
        ):
            if (
                isinstance(sku, bool) or not isinstance(sku, (str, int))
                or not str(sku).strip() or not isinstance(name, str) or not name.strip()
            ):
                continue
            article = str(sku).strip()
            unit_raw = unit.strip() if isinstance(unit, str) else str(unit)
            pack_raw = str(pack)
            duplicate_candidates[article].append((row_num, name, unit_raw, pack_raw))
            if (
                isinstance(pack, bool) or not isinstance(pack, (int, float))
                or pack <= 0 or not float(pack).is_integer()
            ):
                invalid_packs.append(_case(
                    "avt_el_ecoplast_20260615", sheet.title, [row_num],
                    "ecoplast_invalid_package", name, article,
                    observed={"source_unit": unit_raw, "package_quantity_raw": pack_raw},
                ))
        duplicated = [
            (sku, refs) for sku, refs in duplicate_candidates.items() if len(refs) > 1
        ]
        duplicated.sort(key=lambda item: item[1][0][0])
        cases = [
            _case(
                "avt_el_ecoplast_20260615", sheet.title,
                [item[0] for item in refs], "ecoplast_duplicate_sku", refs[0][1], sku,
                observed={"original_unit_values": " | ".join(
                    sorted({item[2] for item in refs})
                ), "original_package_values": " | ".join(
                    sorted({item[3] for item in refs})
                )},
            )
            for sku, refs in duplicated[:limit]
        ]
        cases.extend(invalid_packs[:limit])
        return {
            "ecoplast_duplicate_sku": len(duplicated),
            "ecoplast_invalid_package": len(invalid_packs),
        }, cases
    finally:
        book.close()


def _shveigid(root: Path, limit: int) -> tuple[dict[str, int], list[dict[str, object]]]:
    book = load_workbook(root / EXPECTED["shveigid_20261021"][2],
                         read_only=True, data_only=False, keep_links=False)
    try:
        sheet = book["price"]
        grouped: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        missing: list[dict[str, object]] = []
        for row_num, (item_id, sku, name, price, currency, rub, _) in enumerate(
            sheet.iter_rows(min_row=2, max_col=7, values_only=True), start=2,
        ):
            if (
                currency != "RUB" or not isinstance(name, str) or not name.strip()
                or not _positive_number(price) or not _positive_number(rub)
                or Decimal(str(price)) != Decimal(str(rub))
            ):
                continue
            internal_id = str(item_id).strip()
            article = str(sku).strip() if sku is not None else ""
            if article.lower() in ("", "none", "null"):
                missing.append(_case(
                    "shveigid_20261021", sheet.title, [row_num],
                    "shveigid_missing_article", name, None, internal_id=internal_id,
                ))
            else:
                grouped[article].append((row_num, name, internal_id))
        duplicated = [
            (article, items) for article, items in grouped.items() if len(items) > 1
        ]
        duplicated.sort(key=lambda item: item[1][0][0])
        duplicate_cases = [
            _case(
                "shveigid_20261021", sheet.title,
                [item[0] for item in refs], "shveigid_duplicate_article",
                refs[0][1], article,
                observed={"source_item_ids": " | ".join(ref[2] for ref in refs)},
            )
            for article, refs in duplicated[:limit]
        ]
        return {
            "shveigid_missing_article": len(missing),
            "shveigid_duplicate_article": len(duplicated),
        }, [*_even_sample(missing, limit), *duplicate_cases]
    finally:
        book.close()


def _etk(root: Path, limit: int) -> tuple[dict[str, int], list[dict[str, object]]]:
    path = root / EXPECTED["etk_rezerv_20260316"][2]
    book_values = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    book_formula = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        all_cases: list[dict[str, object]] = []
        for values, formula in zip(
            book_values.worksheets, book_formula.worksheets, strict=True,
        ):
            for row_num, (value_row, formula_row) in enumerate(zip(
                values.iter_rows(min_row=11, max_col=8, values_only=True),
                formula.iter_rows(min_row=11, max_col=8),
                strict=True,
            ), start=11):
                name, cached = value_row[0], value_row[7]
                if (
                    not isinstance(name, str) or not name.strip()
                    or formula_row[7].data_type != "f"
                    or not _positive_number(cached)
                ):
                    continue
                all_cases.append(_case(
                    "etk_rezerv_20260316", values.title, [row_num],
                    "etk_cached_formula_price", name, None,
                    observed={"original_sheet_date": str(values["A8"].value)},
                ))
        return {"etk_cached_formula_price": len(all_cases)}, _even_sample(
            all_cases, limit,
        )
    finally:
        book_values.close()
        book_formula.close()


def _technoas(root: Path, limit: int) -> tuple[dict[str, int], list[dict[str, object]]]:
    book = load_workbook(root / EXPECTED["technoas_20260916"][2],
                         read_only=True, data_only=False, keep_links=False)
    try:
        sheet = book.worksheets[0]
        cases: list[dict[str, object]] = []
        for row_num, (_, _, code, name, _, _, vat) in enumerate(
            sheet.iter_rows(min_row=11, max_col=7), start=11,
        ):
            sku, title = code.value, name.value
            if (
                isinstance(sku, bool) or not isinstance(sku, (str, int))
                or not str(sku).strip()
                or not isinstance(title, str) or not title.strip()
                or vat.data_type == "f" or not _positive_number(vat.value)
            ):
                continue
            cases.append(_case(
                "technoas_20260916", sheet.title, [row_num],
                "technoas_currency_unverified", title, str(sku).strip(),
                observed={"source_header": "Цена с НДС 22%", "currency": "unknown"},
            ))
        return {"technoas_currency_unverified": len(cases)}, _even_sample(
            cases, limit,
        )
    finally:
        book.close()


def build_supplier_quality_triage(
    root: Path = ROOT, *, per_issue: int = 6,
) -> dict[str, object]:
    """Generate source-locked cases. Never mark supplier data safe or reviewed."""
    if isinstance(per_issue, bool) or not 1 <= per_issue <= MAX_CASES_PER_ISSUE:
        raise ValueError("per_issue must be between 1 and 20.")
    audit_recent_supplier_candidates(root)
    counts: dict[str, int] = {}
    cases: list[dict[str, object]] = []
    for build in (_ecoplast, _shveigid, _etk, _technoas):
        issue_counts, selected = build(root, per_issue)
        counts.update(issue_counts)
        cases.extend(selected)
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Source-backed quality case IDs must be distinct.")
    return {
        "schema_version": "supplier-quality-triage-v1",
        "per_issue": per_issue,
        "source_sha256": {source_id: EXPECTED[source_id][3] for source_id in SOURCE_IDS},
        "issue_population_counts": counts,
        "cases": cases,
        "human_reviewed": False,
        "training_authorized": False,
        "current_price_verified": False,
        "canonical_importable": False,
        "reuse_rights_reviewed": False,
        "purpose": "local_human_source_quality_triage_only",
    }


def _verified_original_digests(root: Path) -> tuple[str, ...]:
    """A cache hit still checks every original byte and manifest byte.

    Re-parsing every XLSX on identical sequential reviews is unnecessary,
    but file modification, symlink substitution, or changed manifest must
    invalidate an expensive prior structural audit immediately.
    """
    root = root.resolve()
    manifest = root / "manifest.json"
    if (manifest.is_symlink() or not manifest.is_file()
            or not 0 < manifest.stat().st_size <= 16_384):
        raise ValueError("Supplier research manifest missing or redirected.")
    files = [manifest, *(root / EXPECTED[key][2] for key in sorted(EXPECTED))]
    digests: list[str] = []
    for path in files:
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ValueError("Supplier original missing or redirected.")
        if path != manifest:
            expected = next(data for data in EXPECTED.values() if data[2] == path.name)
            if path.stat().st_size != expected[4]:
                raise ValueError("Supplier original source size changed.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path != manifest and digest != expected[3]:
            raise ValueError("Supplier original source checksum changed.")
        digests.append(digest)
    return tuple(digests)


@lru_cache(maxsize=6)
def _rebuild_verified_packet(
    canonical_root: str, per_issue: int, original_digests: tuple[str, ...],
) -> dict[str, object]:
    # The cache key consists of actual cryptographic content hashes, never
    # of timestamps or file sizes alone; the full original audit runs on miss.
    return build_supplier_quality_triage(Path(canonical_root), per_issue=per_issue)


def verify_supplier_quality_triage(root: Path, packet: object) -> dict[str, object]:
    if not isinstance(packet, dict):
        raise TypeError("Supplier quality packet must be a JSON object.")
    per_issue = packet.get("per_issue")
    if isinstance(per_issue, bool) or not isinstance(per_issue, int):
        raise TypeError("Invalid triage sample size.")
    expected = _rebuild_verified_packet(
        str(root.resolve()), per_issue, _verified_original_digests(root),
    )
    if packet != expected:
        raise ValueError("Supplier source quality queue mismatch or tampering.")
    cases = expected["cases"]
    if not isinstance(cases, list):
        raise TypeError("Rebuilt source quality cases must be a list.")
    return {
        "case_count": len(cases),
        "issue_population_counts": expected["issue_population_counts"],
        "original_sources": len(SOURCE_IDS),
        "training_authorized": False,
        "human_reviewed": False,
    }


def _read_review_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKET_BYTES:
        raise ValueError("Quality review input missing, redirected, or oversized.")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline supplier price quality triage.")
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("build")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--per-issue", type=int, default=6)
    verify = commands.add_parser("verify")
    verify.add_argument("--queue", type=Path, required=True)
    template = commands.add_parser("template")
    template.add_argument("--queue", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--queue", type=Path, required=True)
    validate.add_argument("--decisions", type=Path, required=True)
    consensus = commands.add_parser("consensus")
    consensus.add_argument("--queue", type=Path, required=True)
    consensus.add_argument("--first", type=Path, required=True)
    consensus.add_argument("--second", type=Path, required=True)
    consensus.add_argument("--adjudications", type=Path)
    consensus.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "build":
        packet = build_supplier_quality_triage(per_issue=args.per_issue)
        body = json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(body.encode("utf-8")) > MAX_PACKET_BYTES:
            raise ValueError("Quality review packet exceeds size limit.")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(body)
        cases = packet["cases"]
        if not isinstance(cases, list):
            raise TypeError("Source quality cases must be a list.")
        result = {
            "case_count": len(cases),
            "issue_population_counts": packet["issue_population_counts"],
            "training_authorized": False,
            "human_reviewed": False,
        }
    elif args.action == "verify":
        packet_from_disk = _read_review_json(args.queue)
        result = verify_supplier_quality_triage(ROOT, packet_from_disk)
    else:
        from scripts.supplier_quality_decisions import (
            adjudicate_supplier_quality_batches,
            build_supplier_quality_review_template,
            validate_supplier_quality_decisions,
        )

        packet_from_disk = _read_review_json(args.queue)
        verify_supplier_quality_triage(ROOT, packet_from_disk)
        if not isinstance(packet_from_disk, dict):
            raise TypeError("Quality queue must be a JSON object.")
        if args.action == "template":
            template_records = build_supplier_quality_review_template(packet_from_disk)
            body = json.dumps(
                template_records, ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n"
            if len(body.encode("utf-8")) > MAX_PACKET_BYTES:
                raise ValueError("Quality reviewer template exceeds size limit.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(body)
            result = {
                "case_count": len(template_records),
                "human_reviewed": False,
                "training_authorized": False,
            }
        elif args.action == "validate":
            decisions = _read_review_json(args.decisions)
            if not isinstance(decisions, list):
                raise TypeError("Quality human decisions must be a JSON list.")
            result = dict(validate_supplier_quality_decisions(packet_from_disk, decisions))
        else:
            first = _read_review_json(args.first)
            second = _read_review_json(args.second)
            third = _read_review_json(args.adjudications) if args.adjudications else None
            if (
                not isinstance(first, list) or not isinstance(second, list)
                or (third is not None and not isinstance(third, list))
            ):
                raise TypeError("Quality reviewer batches must be JSON lists.")
            result = adjudicate_supplier_quality_batches(
                ROOT, packet_from_disk, first, second, adjudications=third,
            )
            body = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            if len(body.encode("utf-8")) > MAX_PACKET_BYTES:
                raise ValueError("Quality consensus exceeds size limit.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(body)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
