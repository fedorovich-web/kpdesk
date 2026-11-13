"""A77 250-revision immutable-history and materialized-state contract on clone."""
from __future__ import annotations

import hashlib
import uuid

from canonical_checkpoint_acceptance import PUT
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kpdesk.api.tenant_supplier_checkpoint_page_routes import _PAGE, _PROBE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant


def verify_deep_history(
    tenant: Engine, clone_owner: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, owner: IssuedSession, editor: IssuedSession,
) -> None:
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    scope = {
        "org": organization, "supplier": "supplier-a77-long-history",
        "source": digest("a77-immutable-source"),
        "mapping": digest("a77-mapping"),
        "taxonomy": "a77-long-history-v1", "basis": "standard",
    }
    baseline_reason = "Explicitly excluded after reviewing original supplier section."
    line3 = {
        "line": 3, "source_row_sha256": digest("row-3-original"),
        "action": "exclude", "confirmed": True, "reason": baseline_reason,
    }
    parent: uuid.UUID | None = None
    snapshots = 251
    with tenant.begin() as connection:
        bind_authenticated_tenant(
            connection, session_token=owner.session_token,
            organization_id=organization, operation="write", csrf=owner.csrf_token,
        )
        for revision in range(snapshots):
            changed_reason = f"Human confirmed: current source row is a heading, revision {revision}."
            line2 = {
                "line": 2, "source_row_sha256": digest("row-2-original"),
                "action": "exclude", "confirmed": True, "reason": changed_reason,
            }
            decisions = [line2, line3] if revision == 0 else [line2]
            old_parent = parent
            saved = connection.scalar(PUT, {
                **scope, "idempotency": f"a77-deep-{revision:04d}",
                "expected": parent, "decisions": decisions,
            })
            assert isinstance(saved, uuid.UUID) and saved != old_parent
            parent = saved
            if revision in (0, 198, 199, 200, 249, 250):
                page = connection.execute(
                    _PAGE, {**scope, "offset": 0, "limit": 1},
                ).one()
                assert page.checkpoint_id == saved
                assert page.total_count == 2
                assert len(page.decisions) == 1
                assert page.decisions[0]["line"] == 2
                assert page.decisions[0]["reason"] == changed_reason
                probe = connection.execute(
                    _PROBE, {**scope, "lines": [2, 3, 4]},
                ).one()
                assert probe.checkpoint_id == saved
                assert probe.total_count == 2
                assert probe.overlap_count == 2

        # Replaying the last exact idempotent write may not change the head.
        repeated = connection.scalar(PUT, {
            **scope, "idempotency": "a77-deep-0250",
            "expected": old_parent, "decisions": [line2],
        })
        assert repeated == parent
        assert connection.execute(
            _PAGE, {**scope, "offset": 0, "limit": 1},
        ).one().checkpoint_id == parent

        second_page = connection.execute(
            _PAGE, {**scope, "offset": 1, "limit": 1},
        ).one()
        assert second_page.decisions == [line3]
        assert connection.execute(
            _PAGE, {**scope, "offset": 2, "limit": 1},
        ).one().decisions == []

        # The historic A75/A76 recursive functions return nothing at 201+;
        # the new current-state functions still serve all verified decisions.
        legacy_page = connection.execute(text("""
            SELECT * FROM public.a75_checkpoint_page(
              :org,:supplier,:source,:mapping,:taxonomy,:basis,0,1
            )
        """), scope).one_or_none()
        assert legacy_page is None
        legacy_probe = connection.execute(text("""
            SELECT * FROM public.a76_checkpoint_probe(
              :org,:supplier,:source,:mapping,:taxonomy,:basis,ARRAY[2]::integer[]
            )
        """), scope).one_or_none()
        assert legacy_probe is None

    with tenant.begin() as connection:
        bind_authenticated_tenant(
            connection, session_token=editor.session_token,
            organization_id=organization, operation="read",
        )
        recovered = connection.execute(
            _PAGE, {**scope, "offset": 0, "limit": 50},
        ).one()
        assert recovered.checkpoint_id == parent
        assert [row["line"] for row in recovered.decisions] == [2, 3]
        assert recovered.decisions[0]["reason"] == changed_reason
        assert recovered.decisions[1] == line3

        outsider_scope = {**scope, "org": outsider}
        assert connection.execute(
            _PAGE, {**outsider_scope, "offset": 0, "limit": 50},
        ).one_or_none() is None
        for table in ("a77_current_head", "a77_current_row"):
            assert connection.scalar(text("""
                SELECT has_table_privilege(current_user,:table,'SELECT')
            """), {"table": "public." + table}) is False
            assert connection.scalar(text("""
                SELECT has_table_privilege(current_user,:table,'UPDATE')
            """), {"table": "public." + table}) is False

    # Privileged verification only on the clone: materialized state matches
    # the immutable source audit, and no earlier revision was overwritten.
    with clone_owner.connect() as connection:
        count = connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_checkpoints
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
        """), scope)
        assert count == snapshots
        indexed = connection.execute(text("""
            SELECT checkpoint_id,total_count FROM public.a77_current_head
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
        """), scope).one()
        assert indexed.checkpoint_id == parent and indexed.total_count == 2
        indexed_rows = connection.execute(text("""
            SELECT source_line,source_row_sha256,decision,checkpoint_id
            FROM public.a77_current_row
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
            ORDER BY source_line
        """), scope).all()
        assert len(indexed_rows) == 2
        assert indexed_rows[0].source_row_sha256 == line2["source_row_sha256"]
        assert indexed_rows[0].checkpoint_id == parent
        assert indexed_rows[0].decision == line2
        assert indexed_rows[1].decision == line3
        assert indexed_rows[1].checkpoint_id != parent
        for table in ("a77_current_head", "a77_current_row"):
            constraints: list[str] = [
                str(row[0]) for row in connection.execute(text("""
                    SELECT indexdef FROM pg_indexes
                    WHERE schemaname='public' AND tablename=:table
                """), {"table": table}).all()
            ]
            assert any("UNIQUE" in idx and "organization_id" in idx for idx in constraints)
        for signature in (
            "public.a77_checkpoint_page(uuid,text,text,text,text,text,integer,integer)",
            "public.a77_checkpoint_probe(uuid,text,text,text,text,text,integer[])",
        ):
            assert connection.scalar(text("""
                SELECT has_function_privilege(
                  'kpdesk_auth_runtime',:signature,'EXECUTE'
                )
            """), {"signature": signature}) is False
            assert connection.scalar(text("""
                SELECT has_function_privilege(
                  'kpdesk_tenant_runtime',:signature,'EXECUTE'
                )
            """), {"signature": signature}) is True


def verify_materialized_limit_is_atomic(
    tenant: Engine, clone_owner: Engine,
    organization: uuid.UUID, owner: IssuedSession,
) -> None:
    """A 5001st distinct row must roll back immutable audit AND current index."""
    def digest(item: str) -> str:
        return hashlib.sha256(item.encode("utf-8")).hexdigest()
    scope = {
        "org": organization, "supplier": "supplier-a76-benchmark",
        "source": digest("a76-5000-synthetic-source"),
        "mapping": digest("a76-5000-synthetic-mapping"),
        "taxonomy": "a76-local-benchmark-v1", "basis": "standard",
    }
    with tenant.begin() as connection:
        bind_authenticated_tenant(
            connection, session_token=owner.session_token,
            organization_id=organization, operation="write", csrf=owner.csrf_token,
        )
        original = connection.execute(
            _PROBE, {**scope, "lines": [5000, 5001]},
        ).one()
        assert original.total_count == 5000 and original.overlap_count == 1
        candidate = {
            "line": 5001,
            "source_row_sha256": digest("a77-extra-row-5001"),
            "confirmed": True, "action": "exclude",
            "reason": "Explicitly reviewed extra synthetic section heading.",
        }
        try:
            with connection.begin_nested():
                connection.scalar(PUT, {
                    **scope, "idempotency": "a77-overflow-5001",
                    "expected": original.checkpoint_id, "decisions": [candidate],
                })
        except DBAPIError as exc:
            assert getattr(exc.orig, "sqlstate", None) == "23514"
        else:
            raise AssertionError("A77 allowed 5001 distinct decisions.")
        final = connection.execute(
            _PROBE, {**scope, "lines": [5000, 5001]},
        ).one()
        assert final.checkpoint_id == original.checkpoint_id
        assert final.total_count == 5000 and final.overlap_count == 1

    with clone_owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_checkpoints
            WHERE organization_id=:org AND supplier_id=:supplier
        """), scope) == 100
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a77_current_row
            WHERE organization_id=:org AND supplier_id=:supplier
        """), scope) == 5000
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_row_decisions d
            JOIN public.a72_canonical_checkpoints c
              ON c.checkpoint_id=d.checkpoint_id
            WHERE c.organization_id=:org AND c.supplier_id=:supplier
        """), scope) == 5000
