"""Reproducible, offline, SYNTHETIC catalog parsing and matching benchmark.

This is NOT a production API, SQL, LLM, dataset quality or multi-user load test.
No external network, PostgreSQL writes, provider calls or model training.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import platform
import statistics
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from kpdesk.catalog.parse import parse_price_list
from kpdesk.catalog.schema import COLUMNS, ImportLimits
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import RequestedItem
from kpdesk.quotes.calculate import calculate_line, calculate_quote


@dataclass(frozen=True, slots=True)
class Scenario:
    sku_count: int
    request_count: int
    csv_sha256: str
    import_ms: float
    match_ms: float
    calc_ms: float
    match_p50_ms: float
    match_p95_ms: float
    process_peak_python_alloc_mib: float
    result_lines: int
    result_total_rub: str
    incorrectly_compatible: int
    sql_queries: int


def synthetic_csv(count: int) -> bytes:
    if not 1 <= count <= 5000:
        raise ValueError("sku_count must be 1..5000")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerow(COLUMNS)
    for index in range(count):
        density = 65 if index % 7 == 0 else 80
        writer.writerow((
            f"SYN-{index:05d}",
            f"Бумага офисная A4 {density} г/м2 серия {index:05d}",
            "paper", "пачка", 1, "399.00", "SyntheticBrand",
            "A4", density, "", "",
        ))
    return stream.getvalue().encode("utf-8")


def percentile_ms(values: Sequence[float], proportion: float) -> float:
    if not values or not 0 < proportion <= 1:
        raise ValueError("Missing observations or invalid percentile")
    return sorted(values)[math.ceil(len(values) * proportion) - 1]


def run_scenario(count: int, request_count: int) -> Scenario:
    if not 1 <= request_count <= 20 or count < request_count + 1:
        raise ValueError("Unrepresentative request_count")
    payload = synthetic_csv(count)
    tracemalloc.start()
    try:
        started = time.perf_counter_ns()
        imported = parse_price_list("synthetic.csv", payload, ImportLimits())
        import_ms = (time.perf_counter_ns() - started) / 1e6
        if not imported.publishable or len(imported.products) != count:
            raise RuntimeError(f"Real importer rejected fixture: {imported.errors}")
        source = imported.products
        selected = [item for item in source if item.paper_gsm == 80][:request_count]
        if len(selected) != request_count:
            raise RuntimeError("Missing compatible products")
        times: list[float] = []
        calculations = []
        incorrect = 0
        start_matching = time.perf_counter_ns()
        for selected_product in selected:
            request = RequestedItem(
                description=selected_product.name, category="paper",
                quantity=2, unit="пачка", paper_format="A4", paper_gsm=80,
            )
            t0 = time.perf_counter_ns()
            candidates = retrieve_candidates(request, source, limit=20)
            ranked = rank_candidates(request, candidates, mode="rapidfuzz")
            times.append((time.perf_counter_ns() - t0) / 1e6)
            if not ranked:
                raise RuntimeError("A known match was not retrieved")
            incorrect += sum(item.constraints.status != "compatible" for item in ranked[:1])
            calculations.append((request, ranked[0].product))
        match_ms = (time.perf_counter_ns() - start_matching) / 1e6
        start_calc = time.perf_counter_ns()
        priced = [calculate_line(request, product) for request, product in calculations]
        total = calculate_quote(priced)
        calc_ms = (time.perf_counter_ns() - start_calc) / 1e6
        if total.line_count != request_count or incorrect:
            raise RuntimeError("Portfolio benchmark produced unsafe quote")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return Scenario(
        sku_count=count, request_count=request_count,
        csv_sha256=hashlib.sha256(payload).hexdigest(),
        import_ms=round(import_ms, 3),
        match_ms=round(match_ms, 3),
        calc_ms=round(calc_ms, 3),
        match_p50_ms=round(statistics.median(times), 3),
        match_p95_ms=round(percentile_ms(times, .95), 3),
        process_peak_python_alloc_mib=round(peak / (1024**2), 3),
        result_lines=total.line_count,
        result_total_rub=str(total.total_rub),
        incorrectly_compatible=incorrect,
        sql_queries=0,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Refusing to overwrite a previous benchmark report")
    scenarios = [
        asdict(run_scenario(count, requests))
        for count in (1000, 5000) for requests in (5, 20)
    ]
    report = {
        "kind": "synthetic_offline_reproducible_microbenchmark",
        "production_eligible": False,
        "price_and_accuracy_claims": False,
        "database": "not used; sql_queries=0 by design; not a DB benchmark",
        "llm": "not used; cost/latency unknown",
        "performance_scope": "single local process; Python allocations via tracemalloc, not RSS",
        "sample_sizes": "one measurement per scenario; p95 over 5/20 request operations",
        "python_version": platform.python_version(),
        "windows_or_os": platform.system(),
        "scenarios": scenarios,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
