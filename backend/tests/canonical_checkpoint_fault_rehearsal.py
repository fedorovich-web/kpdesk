"""A78 clone-only transactional fault and concurrency acceptance for A77.

Uses two independent PostgreSQL runtime connections with real tenant sessions.
Tests never execute on the working kpdesk database.
"""
from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from canonical_checkpoint_acceptance import PUT
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from kpdesk.api.tenant_supplier_checkpoint_page_routes import _PAGE, _PROBE
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decision(version: str) -> dict[str, Any]:
    return {
        "line": 2,
        "source_row_sha256": _sha("a78-source-row-2"),
        "action": "exclude",
        "confirmed": True,
        "reason": f"Explicitly reviewed supplier heading: {version}.",
    }


def _current_scope(organization: uuid.UUID) -> dict[str, Any]:
    return {
        "org": organization,
        "supplier": "supplier-a78-rollback",
        "source": _sha("a78-original-source"),
        "mapping": _sha("a78-mapping-definition"),
        "taxonomy": "a78-clone-contract",
        "basis": "standard",
    }


def _put(
    scope: dict[str, Any], key: str, parent: uuid.UUID | None, version: str,
) -> dict[str, Any]:
    return {
        **scope,
        "idempotency": key,
        "expected": parent,
        "decisions": [_decision(version)],
    }


def _bind(connection: Connection, organization: uuid.UUID, session: IssuedSession) -> None:
    bind_authenticated_tenant(
        connection,
        session_token=session.session_token,
        organization_id=organization,
        operation="write",
        csrf=session.csrf_token,
    )


def _verify_projection(owner: Engine, scope: dict[str, Any], expected: uuid.UUID) -> None:
    """Compare the mutable read model against the immutable audit, not only counts."""
    with owner.connect() as connection:
        head = connection.execute(text("""
            SELECT checkpoint_id,total_count,previous_id
            FROM public.a77_current_head
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
        """), scope).one()
        assert head.checkpoint_id == expected
        recent = connection.execute(text("""
            SELECT checkpoint_id,previous_id
            FROM public.a72_canonical_checkpoints
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
            ORDER BY created_at DESC,checkpoint_id DESC LIMIT 1
        """), scope).one()
        assert head.checkpoint_id == recent.checkpoint_id
        assert head.previous_id == recent.previous_id

        audit_rows = connection.execute(text("""
            SELECT DISTINCT ON (d.source_line)
              d.source_line,d.source_row_sha256,d.decision,d.checkpoint_id
            FROM public.a72_canonical_row_decisions d
            JOIN public.a72_canonical_checkpoints c
              ON c.checkpoint_id=d.checkpoint_id
            WHERE c.organization_id=:org AND c.supplier_id=:supplier
              AND c.source_sha256=:source AND c.mapping_sha256=:mapping
              AND c.taxonomy_version=:taxonomy AND c.price_basis=:basis
            ORDER BY d.source_line,c.created_at DESC,c.checkpoint_id DESC
        """), scope).all()
        materialized = connection.execute(text("""
            SELECT source_line,source_row_sha256,decision,checkpoint_id
            FROM public.a77_current_row
            WHERE organization_id=:org AND supplier_id=:supplier
              AND source_sha256=:source AND mapping_sha256=:mapping
              AND taxonomy_version=:taxonomy AND price_basis=:basis
            ORDER BY source_line
        """), scope).all()
        assert len(audit_rows) == len(materialized) == head.total_count
        assert [tuple(row) for row in audit_rows] == [
            tuple(row) for row in materialized
        ]
        # Foreign keys constrain both the audit revision's organization and
        # the row's exact current-state scope, independent of app code.
        assert connection.scalar(text("""
            SELECT count(*) FROM pg_constraint
            WHERE conrelid='public.a77_current_head'::regclass
              AND contype='f'
        """)) >= 3
        assert connection.scalar(text("""
            SELECT count(*) FROM pg_constraint
            WHERE conrelid='public.a77_current_row'::regclass
              AND contype='f'
        """)) >= 2


def verify_checkpoint_fault_and_races(
    tenant: Engine, clone_owner: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, session: IssuedSession,
) -> None:
    """All injected constraints and review records live only in the marked clone."""
    scope = _current_scope(organization)
    with tenant.begin() as connection:
        _bind(connection, organization, session)
        original = connection.scalar(PUT, _put(scope, "a78-first-confirmed", None, "first"))
        assert isinstance(original, uuid.UUID)
        assert connection.execute(
            _PAGE, {**scope, "offset": 0, "limit": 5},
        ).one().decisions == [_decision("first")]

    # Force failure after immutable checkpoint rows are inserted but while
    # their statement-level projection trigger updates the current table.
    with clone_owner.begin() as connection:
        connection.execute(text("""
            ALTER TABLE public.a77_current_row
            ADD CONSTRAINT a78_inject_projection_failure
            CHECK (decision->>'reason' <> 'Explicitly reviewed supplier heading: injected.')
        """))
    try:
        with tenant.begin() as connection:
            _bind(connection, organization, session)
            try:
                with connection.begin_nested():
                    connection.scalar(PUT, _put(
                        scope, "a78-fault-retry-key", original, "injected",
                    ))
            except DBAPIError as error:
                assert getattr(error.orig, "sqlstate", None) == "23514"
            else:
                raise AssertionError("Projection fault injection did not abort the audit.")
            assert connection.execute(
                _PAGE, {**scope, "offset": 0, "limit": 5},
            ).one().checkpoint_id == original
            assert connection.execute(
                _PROBE, {**scope, "lines": [2]},
            ).one().total_count == 1
        _verify_projection(clone_owner, scope, original)
        with clone_owner.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM public.a72_canonical_checkpoints
                WHERE organization_id=:org AND supplier_id=:supplier
            """), scope) == 1
    finally:
        with clone_owner.begin() as connection:
            connection.execute(text("""
                ALTER TABLE public.a77_current_row
                DROP CONSTRAINT a78_inject_projection_failure
            """))

    # After rollback the failed idempotency key must not be reserved.
    with tenant.begin() as connection:
        _bind(connection, organization, session)
        recovered = connection.scalar(PUT, _put(
            scope, "a78-fault-retry-key", original, "recovered",
        ))
        assert isinstance(recovered, uuid.UUID) and recovered != original
    _verify_projection(clone_owner, scope, recovered)

    # A valid write aborted by the caller must roll back both records.
    with tenant.begin() as connection:
        _bind(connection, organization, session)
        try:
            with connection.begin_nested():
                staged = connection.scalar(PUT, _put(
                    scope, "a78-client-rollback", recovered, "aborted",
                ))
                assert isinstance(staged, uuid.UUID)
                assert connection.execute(
                    _PAGE, {**scope, "offset": 0, "limit": 5},
                ).one().checkpoint_id == staged
                raise RuntimeError("A78 simulated client transaction abort")
        except RuntimeError as error:
            assert str(error) == "A78 simulated client transaction abort"
        assert connection.execute(
            _PAGE, {**scope, "offset": 0, "limit": 5},
        ).one().checkpoint_id == recovered
    _verify_projection(clone_owner, scope, recovered)

    def race(
        barrier: Barrier, key: str, previous: uuid.UUID,
        version: str,
    ) -> tuple[str, uuid.UUID | str]:
        try:
            with tenant.begin() as connection:
                _bind(connection, organization, session)
                barrier.wait(timeout=15)
                checkpoint = connection.scalar(PUT, _put(scope, key, previous, version))
                assert isinstance(checkpoint, uuid.UUID)
                return ("ok", checkpoint)
        except DBAPIError as error:
            return ("conflict", str(getattr(error.orig, "sqlstate", None)))

    distinct_barrier = Barrier(2, timeout=15)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            race, distinct_barrier, "a78-race-first", recovered, "racing-first",
        )
        second_future = executor.submit(
            race, distinct_barrier, "a78-race-second", recovered, "racing-second",
        )
        results = [first_future.result(timeout=30), second_future.result(timeout=30)]
    assert sorted(item[0] for item in results) == ["conflict", "ok"]
    assert next(value for kind, value in results if kind == "conflict") == "40001"
    winner = next(value for kind, value in results if kind == "ok")
    assert isinstance(winner, uuid.UUID)
    _verify_projection(clone_owner, scope, winner)

    repeat_barrier = Barrier(2, timeout=15)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            race, repeat_barrier, "a78-idempotent-same", winner, "identical",
        )
        second_future = executor.submit(
            race, repeat_barrier, "a78-idempotent-same", winner, "identical",
        )
        results = [first_future.result(timeout=30), second_future.result(timeout=30)]
    assert [item[0] for item in results] == ["ok", "ok"]
    same_uuid = results[0][1]
    assert isinstance(same_uuid, uuid.UUID) and same_uuid != winner
    assert results[1][1] == same_uuid
    _verify_projection(clone_owner, scope, same_uuid)

    with tenant.begin() as connection:
        _bind(connection, organization, session)
        latest = connection.execute(
            _PAGE, {**scope, "offset": 0, "limit": 5},
        ).one()
        assert latest.checkpoint_id == same_uuid
        assert latest.total_count == 1
        assert latest.decisions == [_decision("identical")]
        assert connection.execute(
            _PAGE, {**scope, "org": outsider, "offset": 0, "limit": 5},
        ).one_or_none() is None

    # Exactly first + recovered + one winner + one idempotent replay.
    with clone_owner.connect() as connection:
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_checkpoints
            WHERE organization_id=:org AND supplier_id=:supplier
        """), scope) == 4
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_row_decisions d
            JOIN public.a72_canonical_checkpoints c
              ON c.checkpoint_id=d.checkpoint_id
            WHERE c.organization_id=:org AND c.supplier_id=:supplier
        """), scope) == 4
