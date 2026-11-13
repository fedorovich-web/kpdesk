"""P4 measured HTTPS TestClient + PostgreSQL clone performance, not an SLA.

Read-only HTTP requests, separate TestClient per worker, restricted PostgreSQL
roles. Only an explicitly provenance-marked disposable database is eligible.
"""
from __future__ import annotations

import ctypes
import json
import math
import platform
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from ctypes import Structure, byref, c_size_t, c_ulong, sizeof, wintypes
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text

from kpdesk.api.tenant_session_routes import SESSION_COOKIE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone

ROOT = Path(__file__).resolve().parents[2]
_LOADS = (1, 5, 20)
_SAMPLES_PER_CLIENT = 2


class _ProcessMemoryCounters(Structure):
    _fields_ = [
        ("cb", c_ulong), ("PageFaultCount", c_ulong),
        ("PeakWorkingSetSize", c_size_t), ("WorkingSetSize", c_size_t),
        ("QuotaPeakPagedPoolUsage", c_size_t),
        ("QuotaPagedPoolUsage", c_size_t),
        ("QuotaPeakNonPagedPoolUsage", c_size_t),
        ("QuotaNonPagedPoolUsage", c_size_t),
        ("PagefileUsage", c_size_t), ("PeakPagefileUsage", c_size_t),
    ]


def _resident_mib() -> float | None:
    """Actual process working set on Windows (not just tracemalloc allocations)."""
    if sys.platform != "win32":
        return None
    counters = _ProcessMemoryCounters()
    counters.cb = sizeof(counters)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(handle, byref(counters), counters.cb)
    if not ok:
        raise OSError("Win32 GetProcessMemoryInfo failed")
    return float(round(counters.WorkingSetSize / 1048576, 3))


def _quantile(samples: list[float], fraction: float) -> float:
    if not samples or not 0 < fraction <= 1 or any(
        not math.isfinite(value) or value < 0 for value in samples
    ):
        raise ValueError("Invalid latency observations or fraction")
    ordered = sorted(samples)
    return round(ordered[math.ceil(len(ordered) * fraction) - 1], 3)


def _plan_summary(result: object) -> dict[str, object]:
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError("EXPLAIN must return one JSON document")
    document = result[0]
    if not isinstance(document, dict) or not isinstance(document.get("Plan"), dict):
        raise TypeError("EXPLAIN lacks plan data")
    indexes: set[str] = set()
    def walk(node: dict[str, Any]) -> None:
        name = node.get("Index Name")
        if isinstance(name, str):
            indexes.add(name)
        children = node.get("Plans")
        if isinstance(children, list):
            for item in children:
                if isinstance(item, dict):
                    walk(item)
    plan = document["Plan"]
    walk(plan)
    elapsed = document.get("Execution Time")
    if not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise ValueError("EXPLAIN runtime missing")
    return {
        "root": plan.get("Node Type"), "index_names": sorted(indexes),
        "actual_rows": plan.get("Actual Rows"),
        "execution_ms": round(elapsed, 3),
        "shared_hit_blocks": plan.get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan.get("Shared Read Blocks", 0),
    }


def _sql_family(statement: str) -> str:
    """Classify static SQL templates; never retain SQL text or bind parameters."""
    normalized = " ".join(statement.lower().split())
    if "kpdesk_auth_session(" in normalized:
        return "session_validation"
    if "kpdesk_session_tenant_allowed(" in normalized and "to_regprocedure" not in normalized:
        return "tenant_membership"
    if "pg_catalog.pg_database" in normalized and "to_regprocedure" in normalized:
        return "database_environment"
    if "set_config(" in normalized:
        return "transaction_scope"
    if normalized.startswith("set local role "):
        return "role_setup"
    if "from public.quote_documents d" in normalized:
        return "quote_list_join"
    if "from quote_documents" in normalized:
        return "quote_head"
    if "from quote_revisions" in normalized:
        return "quote_revision"
    return "other"


def _sql_timing_summary(
    samples: dict[tuple[str, str, str], list[float]],
) -> dict[str, dict[str, dict[str, dict[str, int | float]]]]:
    """Aggregate event timings by role / endpoint / operation, without raw SQL."""
    result: dict[str, dict[str, dict[str, dict[str, int | float]]]] = {}
    for (role, endpoint, family), durations in sorted(samples.items()):
        if not durations:
            raise ValueError("SQL timing group must contain samples")
        result.setdefault(role, {}).setdefault(endpoint, {})[family] = {
            "count": len(durations),
            "dbapi_execute_total_ms": round(sum(durations), 3),
            "dbapi_execute_p50_ms": _quantile(durations, .5),
            "dbapi_execute_p95_ms": _quantile(durations, .95),
            "dbapi_execute_max_ms": _quantile(durations, 1),
        }
    return result


def benchmark_https_quote_reads(
    *,
    owner: Engine, auth: Engine, tenant: Engine,
    organization: uuid.UUID, session: IssuedSession, quote_id: str,
    factory: Callable[[str], FastAPI],
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Run after normal E2E has created its quote, before disposing the clone."""
    with owner.connect() as connection:
        if not is_live_accounts_http_clone(connection) or (
            connection.scalar(text("SELECT current_user")) != "kpdesk"
            or connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            != "0030_tenant_llm_cache"
        ):
            raise RuntimeError("P4 requires marked disposable PostgreSQL schema 0030")
        scope = {"org": organization, "quote": uuid.UUID(quote_id)}
        # EXPLAIN is executed only in the clone, never in working database.
        query_plans = {
            "quote_latest_revision": _plan_summary(connection.scalar(text("""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT number, total_rub
                FROM public.quote_revisions
                WHERE organization_id=:org AND quote_id=:quote
                ORDER BY number DESC LIMIT 1
            """), scope)),
            "quote_document_lookup": _plan_summary(connection.scalar(text("""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT id, current_revision FROM public.quote_documents
                WHERE organization_id=:org AND id=:quote
            """), scope)),
        }
    with tenant.connect() as connection:
        if not is_live_accounts_http_clone(connection) or (
            connection.scalar(text("SELECT current_user")) != "kpdesk_tenant_runtime"
        ):
            raise RuntimeError("P4 tenant connection is not the restricted clone role")
    with auth.connect() as connection:
        if not is_live_accounts_http_clone(connection) or (
            connection.scalar(text("SELECT current_user")) != "kpdesk_auth_runtime"
        ):
            raise RuntimeError("P4 auth connection is not the restricted clone role")

    urls = {
        "quote_list": f"/api/session/organizations/{organization}/quotes",
        "quote_pdf": f"/api/session/organizations/{organization}/quotes/{quote_id}/pdf?revision=1",
    }
    observations: list[dict[str, object]] = []
    process_before = _resident_mib()
    active_endpoint: ContextVar[str | None] = ContextVar('p4_endpoint', default=None)
    for workers in _LOADS:
        # Build each FastAPI app's lazy route cache serially. Concurrent first
        # requests can race while Pydantic TypeAdapters are being constructed,
        # corrupting global warning filters and contaminating latency samples.
        prepared_apps = tuple(factory("https://testserver") for _ in range(workers))
        for prepared_app in prepared_apps:
            with TestClient(prepared_app, base_url="https://testserver") as client:
                client.cookies.set(SESSION_COOKIE, session.session_token)
                for kind, url in urls.items():
                    warmup = client.get(url)
                    if warmup.status_code != 200:
                        raise AssertionError(
                            f"P4 warmup {kind} rejected ({warmup.status_code})"
                        )
                    if kind == "quote_pdf":
                        if not warmup.content.startswith(b"%PDF-"):
                            raise AssertionError("P4 warmup PDF is not valid")
                    elif not isinstance(warmup.json().get("items"), list):
                        raise AssertionError("P4 warmup quote list response invalid")
        sql_queries = 0
        lock = threading.Lock()
        counted: dict[str, Counter[str]] = {"auth": Counter(), "tenant": Counter()}
        pending: dict[int, tuple[int, str, str, str]] = {}
        query_timings: dict[tuple[str, str, str], list[float]] = defaultdict(list)

        def instrument(
            role: str, *, guard: threading.Lock = lock,
            counters: dict[str, Counter[str]] = counted,
            in_flight: dict[int, tuple[int, str, str, str]] = pending,
        ) -> Callable[..., None]:
            def on_sql(_conn: object, _cursor: object, statement: str,
                       _parameters: object, context: object, _many: bool) -> None:
                nonlocal sql_queries
                endpoint = active_endpoint.get() or "unattributed"
                family = _sql_family(statement)
                started_ns = time.perf_counter_ns()
                with guard:
                    if id(context) in in_flight:
                        raise AssertionError("P4 SQL execution context reused before completion")
                    in_flight[id(context)] = (started_ns, role, endpoint, family)
                    sql_queries += 1
                    counters[role][endpoint] += 1
            return on_sql

        def instrument_end(
            _conn: object, _cursor: object, _statement: str, _parameters: object,
            context: object, _many: bool, *,
            guard: threading.Lock = lock,
            in_flight: dict[int, tuple[int, str, str, str]] = pending,
            samples: dict[tuple[str, str, str], list[float]] = query_timings,
        ) -> None:
            ended_ns = time.perf_counter_ns()
            with guard:
                started_ns, role, endpoint, family = in_flight.pop(id(context))
                samples[(role, endpoint, family)].append(
                    (ended_ns - started_ns) / 1e6
                )

        auth_listener = instrument("auth")
        tenant_listener = instrument("tenant")
        event.listen(auth, "before_cursor_execute", auth_listener)
        event.listen(tenant, "before_cursor_execute", tenant_listener)
        event.listen(auth, "after_cursor_execute", instrument_end)
        event.listen(tenant, "after_cursor_execute", instrument_end)
        try:
            def run_worker(
                worker_index: int, apps: tuple[FastAPI, ...] = prepared_apps,
            ) -> list[tuple[str, float]]:
                measured: list[tuple[str, float]] = []
                # Every worker uses its own serially prewarmed FastAPI app.
                with TestClient(
                    apps[worker_index],
                    base_url="https://testserver",
                ) as client:
                    client.cookies.set(SESSION_COOKIE, session.session_token)
                    for _ in range(_SAMPLES_PER_CLIENT):
                        for kind, url in urls.items():
                            endpoint_token = active_endpoint.set(kind)
                            try:
                                start = time.perf_counter_ns()
                                response = client.get(url)
                                duration = (time.perf_counter_ns() - start) / 1e6
                            finally:
                                active_endpoint.reset(endpoint_token)
                            if response.status_code != 200:
                                raise AssertionError(
                                    f"P4 HTTP {kind} rejected ({response.status_code})"
                                )
                            if kind == "quote_pdf":
                                if not response.content.startswith(b"%PDF-"):
                                    raise AssertionError("P4 revision PDF is not valid")
                            elif not isinstance(response.json().get("items"), list):
                                raise AssertionError("P4 quote list response invalid")
                            measured.append((kind, duration))
                return measured

            start_group = time.perf_counter_ns()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                result_sets = list(pool.map(run_worker, range(workers)))
            elapsed_group = (time.perf_counter_ns() - start_group) / 1e6
        finally:
            event.remove(auth, "before_cursor_execute", auth_listener)
            event.remove(tenant, "before_cursor_execute", tenant_listener)
            event.remove(auth, "after_cursor_execute", instrument_end)
            event.remove(tenant, "after_cursor_execute", instrument_end)

        if pending or sum(map(len, query_timings.values())) != sql_queries:
            raise AssertionError("P4 SQL event tracing lost an execution")
        combined = [item for result in result_sets for item in result]
        expected_requests = workers * _SAMPLES_PER_CLIENT * len(urls)
        if len(combined) != expected_requests or sql_queries < expected_requests:
            raise AssertionError("P4 HTTP requests/SQL calls inconsistent")
        by_role = {role: dict(by_endpoint) for role, by_endpoint in counted.items()}
        if sum(sum(counts.values()) for counts in by_role.values()) != sql_queries:
            raise AssertionError("P4 SQL instrumentation lost a statement")
        # Contract budget, not a throughput claim. A future extra mandatory
        # security check must be reviewed explicitly, never silently removed.
        operations_per_route = workers * _SAMPLES_PER_CLIENT
        for kind, ceiling in (("quote_list", 10), ("quote_pdf", 11)):
            observed = by_role["tenant"].get(kind, 0)
            if observed > operations_per_route * ceiling:
                raise AssertionError(
                    f"P4 SQL query regression for {kind}: "
                    f"{observed}/{operations_per_route} > {ceiling}"
                )
        by_kind: dict[str, dict[str, float | int]] = {}
        for kind in urls:
            samples = [duration for label, duration in combined if label == kind]
            by_kind[kind] = {
                "samples": len(samples), "p50_ms": _quantile(samples, .5),
                "p95_ms": _quantile(samples, .95),
                "max_ms": _quantile(samples, 1),
            }
        observations.append({
            "concurrent_clients": workers,
            "requests": expected_requests,
            "database_statements": sql_queries,
            "sql_statements_per_request": round(sql_queries / expected_requests, 3),
            "sql_by_role_and_route": by_role,
            "sql_dbapi_execute_by_role_route_family": _sql_timing_summary(query_timings),
            "group_wall_ms": round(elapsed_group, 3),
            "throughput_requests_per_sec": round(expected_requests * 1000 / elapsed_group, 3),
            "http": by_kind,
            "process_rss_after_mib": _resident_mib(),
        })
    result: dict[str, object] = {
        "kind": "p4_local_https_testclient_postgresql_clone",
        "production_sla": False,
        "requests_use_real_fastapi_sqlalchemy_pg": True,
        "database_role": "auth and tenant restricted PostgreSQL runtime roles",
        "source": "pre-existing 1-revision synthetic E2E quote",
        "route_names": sorted(urls),
        "request_body_size": "read-only GET; no import, inference or PDF generation writes",
        "sql_count_method": "SQLAlchemy before_cursor_execute on auth+tenant engines",
        "sql_timing_method": "Paired SQLAlchemy before/after_cursor_execute using perf_counter_ns; DB-API execute duration, excludes result fetch, response serialization and TCP/TLS; instrumentation adds overhead; no SQL strings or bound values retained",
        "concurrency_note": "separate FastAPI TestClient per worker; no TCP/TLS, reverse proxy or network",
        "warmup": "Each worker app was warmed serially using real authenticated quote-list and PDF GETs before the measured requests; no cold-cache baseline",
        "python": platform.python_version(), "os": platform.system(),
        "process_rss_before_mib": process_before,
        "postgres_explain_analyze_buffers": query_plans,
        "scenarios": observations,
    }
    if output_dir is not None:
        if not output_dir.is_dir() or output_dir.is_symlink():
            raise RuntimeError("P4 evidence output directory missing or unsafe")
        filename = output_dir / ("p4-https-" + uuid.uuid4().hex + ".json")
        with filename.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
        print("P4_EVIDENCE_PATH " + str(filename))
    print("P4_HTTPS_PG_PERFORMANCE " + json.dumps(result, ensure_ascii=False))
    return result
