"""A72 contract tests for checkpoint persistence ONLY on a disposable PostgreSQL clone."""
from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from canonical_checkpoint_clone_schema import install_checkpoint_prototype
from sqlalchemy import Engine, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from kpdesk.catalog.supplier_canonical_preview import source_row_sha256
from kpdesk.catalog.supplier_staging import SupplierMapping, stage_supplier_prices
from kpdesk.db.auth_sessions import IssuedSession
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant

SOURCE = (
    b"sku;name;price;currency;category;unit;pack\n"
    b"KB-01;Wired keyboard;1250.00;RUB;keyboards;pcs;1\n"
    b"KB-02;Second keyboard;1790.00;RUB;keyboards;pcs;1\n"
    b"Section: accessories\n"
)
MAP = SupplierMapping(
    sku=0, name=1, price=2, currency=3, category=4, sale_unit=5,
    pieces_per_sale_unit=6, header_row=1,
)
STAGED = stage_supplier_prices("supplier.csv", SOURCE, MAP)
SRC = STAGED.sha256
MAPPING = hashlib.sha256(
    json.dumps({
        "sku": 0, "name": 1, "price": 2, "currency": 3, "category": 4,
        "sale_unit": 5, "pieces_per_sale_unit": 6, "header_row": 1,
    }, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
PUT = text("""
    SELECT public.a72_checkpoint_put(
        :org,:supplier,:source,:mapping,:taxonomy,:basis,
        :idempotency,:expected,:decisions
    )
""").bindparams(bindparam("decisions", type_=JSONB))
GET = text("""
    SELECT checkpoint_id,reviewer_user_id,previous_id,taxonomy_version,
           price_basis,decisions
    FROM public.a72_checkpoint_get(:org,:supplier,:source,:mapping,:taxonomy,:basis)
""")


def _decisions() -> list[dict[str, Any]]:
    return [
        {
            "line": row.line, "source_row_sha256": source_row_sha256(row),
            "confirmed": True,
            "action": "exclude" if not row.name else "include",
            **({"reason": "A source section heading is not a sellable product."}
               if not row.name else {
                   "category": "keyboard", "sale_unit": "штука",
                   "pieces_per_sale_unit": 1,
                   "attributes": {"connection": "wired", "keys": 104},
               }),
        }
        for row in STAGED.rows
    ]


def _args(
    org: uuid.UUID, key: str, expected: uuid.UUID | None,
    decisions: list[dict[str, Any]] | None = None,
    source: str = SRC,
    mapping: str = MAPPING,
) -> dict[str, Any]:
    return {
        "org": org, "supplier": "supplier-a72", "source": source,
        "mapping": mapping, "taxonomy": "a72-18-category-v1",
        "basis": "standard", "idempotency": key, "expected": expected,
        "decisions": decisions if decisions is not None else _decisions(),
    }


def _read_args(
    org: uuid.UUID, source: str = SRC, mapping: str = MAPPING,
    taxonomy: str = "a72-18-category-v1", basis: str = "standard",
) -> dict[str, Any]:
    return {"org": org, "supplier": "supplier-a72", "source": source,
            "mapping": mapping, "taxonomy": taxonomy, "basis": basis}


def verify_checkpoint_clone_contract(
    clone_owner: Engine, tenant_runtime: Engine, organization: uuid.UUID,
    outsider: uuid.UUID, owner_session: IssuedSession,
    editor_session: IssuedSession, owner_user: uuid.UUID,
) -> None:
    """Prototype test data is destroyed with the caller's marked clone."""
    with clone_owner.begin() as connection:
        install_checkpoint_prototype(connection)

    with tenant_runtime.begin() as connection:
        assert connection.scalar(text("SELECT current_user")) == "kpdesk_tenant_runtime"
        for table in ("a72_canonical_checkpoints", "a72_canonical_row_decisions"):
            assert connection.scalar(text("""
                SELECT has_table_privilege(current_user,:table,'SELECT')
            """), {"table": "public." + table}) is False
            assert connection.scalar(text("""
                SELECT has_table_privilege(current_user,:table,'INSERT')
            """), {"table": "public." + table}) is False
        assert connection.scalar(GET, _read_args(organization)) is None
        assert connection.scalar(PUT, _args(
            organization, "first000", None,
        )) is None

    with tenant_runtime.begin() as connection:
        identity = bind_authenticated_tenant(
            connection, session_token=owner_session.session_token,
            organization_id=organization, operation="write",
            csrf=owner_session.csrf_token,
        )
        assert identity.identity.user_id == owner_user
        first: uuid.UUID = connection.scalar(PUT, _args(
            organization, "checkpoint-001", None,
        ))
        assert isinstance(first, uuid.UUID)
        repeat = connection.scalar(PUT, _args(
            organization, "checkpoint-001", None,
        ))
        assert repeat == first

        loaded = connection.execute(GET, _read_args(organization)).one()
        assert loaded.checkpoint_id == first
        assert loaded.reviewer_user_id == owner_user
        assert loaded.price_basis == "standard"
        assert loaded.previous_id is None
        assert len(loaded.decisions) == len(STAGED.rows) == 3
        reference = {r.line: source_row_sha256(r) for r in STAGED.rows}
        assert all(
            reference.get(r["line"]) == r["source_row_sha256"]
            and r["confirmed"] is True
            for r in loaded.decisions
        )
        assert connection.scalar(GET, _read_args(
            organization, source="e" * 64,
        )) is None
        assert connection.scalar(GET, _read_args(
            organization, mapping="f" * 64,
        )) is None

        # Spoofing an outsider org while bound to this tenant must disclose nothing.
        assert connection.scalar(GET, _read_args(outsider)) is None
        assert connection.scalar(PUT, _args(
            outsider, "outsider00", None,
        )) is None

        modified = _decisions()
        modified[0]["category"] = "mouse"
        try:
            with connection.begin_nested():
                connection.scalar(PUT, _args(
                    organization, "checkpoint-001", None, modified,
                ))
        except DBAPIError as exc:
            assert getattr(exc.orig, "sqlstate", None) == "23505"
        else:
            raise AssertionError("Idempotency key accepted different decisions.")

        changed_basis = _args(organization, "checkpoint-001", None)
        changed_basis["basis"] = "retail"
        try:
            with connection.begin_nested():
                connection.scalar(PUT, changed_basis)
        except DBAPIError as exc:
            assert getattr(exc.orig, "sqlstate", None) == "23505"
        else:
            raise AssertionError("Idempotent checkpoint changed its price basis.")

        try:
            with connection.begin_nested():
                connection.scalar(PUT, _args(
                    organization, "checkpoint-002", None,
                ))
        except DBAPIError as exc:
            assert getattr(exc.orig, "sqlstate", None) == "40001"
        else:
            raise AssertionError("Competing draft overwrote the saved checkpoint.")

        second = connection.scalar(PUT, _args(
            organization, "checkpoint-002", first,
        ))
        assert isinstance(second, uuid.UUID) and second != first
        latest = connection.execute(GET, _read_args(organization)).one()
        assert latest.checkpoint_id == second
        assert latest.previous_id == first
        assert latest.decisions == loaded.decisions

        invalid = _decisions()
        invalid[1]["source_row_sha256"] = "not-a-digest"
        assert connection.scalar(PUT, _args(
            organization, "checkpoint-003", second, invalid,
        )) is None
        unchecked = _decisions()
        unchecked[0]["confirmed"] = "true"  # string, not genuine consent
        assert connection.scalar(PUT, _args(
            organization, "checkpoint-004", second, unchecked,
        )) is None
        duplicated = _decisions()
        duplicated[2] = duplicated[1].copy()
        assert connection.scalar(PUT, _args(
            organization, "checkpoint-005", second, duplicated,
        )) is None
        missing_commercial_facts = _decisions()
        del missing_commercial_facts[0]["category"]
        assert connection.scalar(PUT, _args(
            organization, "checkpoint-006", second, missing_commercial_facts,
        )) is None
        assert connection.scalar(GET, _read_args(organization)) == second
        assert connection.scalar(GET, _read_args(
            organization, taxonomy="next-taxonomy-v2",
        )) is None
        assert connection.scalar(GET, _read_args(
            organization, basis="retail",
        )) is None
        next_taxonomy = _args(organization, "checkpoint-taxonomy-new", None)
        next_taxonomy["taxonomy"] = "next-taxonomy-v2"
        third = connection.scalar(PUT, next_taxonomy)
        assert isinstance(third, uuid.UUID) and third != second
        assert connection.scalar(GET, _read_args(organization)) == second
        assert connection.scalar(GET, _read_args(
            organization, taxonomy="next-taxonomy-v2",
        )) == third

    with tenant_runtime.begin() as connection:
        identity = bind_authenticated_tenant(
            connection, session_token=editor_session.session_token,
            organization_id=organization, operation="read",
        )
        assert identity.identity.user_id != owner_user
        # Author was taken from the original server-bound session, not JSON.
        recovered = connection.execute(GET, _read_args(organization)).one()
        assert recovered.checkpoint_id == second
        assert recovered.reviewer_user_id == owner_user
        assert recovered.decisions == _decisions()

    # A74: two real connections racing against the SAME expected revision.
    # Advisory transaction locking must serialize writes: exactly one succeeds.
    start = Barrier(2, timeout=12)

    def race(key: str, expected: uuid.UUID) -> tuple[str, uuid.UUID | str]:
        try:
            with tenant_runtime.begin() as connection:
                bind_authenticated_tenant(
                    connection, session_token=owner_session.session_token,
                    organization_id=organization, operation="write",
                    csrf=owner_session.csrf_token,
                )
                start.wait()
                written = connection.scalar(PUT, _args(organization, key, expected))
                assert isinstance(written, uuid.UUID)
                return ("created", written)
        except DBAPIError as error:
            return ("conflict", str(getattr(error.orig, "sqlstate", None)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_race = pool.submit(race, "checkpoint-race-a", second)
        second_race = pool.submit(race, "checkpoint-race-b", second)
        outcomes = [first_race.result(timeout=30), second_race.result(timeout=30)]
    assert sorted(kind for kind, _ in outcomes) == ["conflict", "created"]
    assert next(value for kind, value in outcomes if kind == "conflict") == "40001"
    winner = next(value for kind, value in outcomes if kind == "created")
    assert isinstance(winner, uuid.UUID)
    with tenant_runtime.begin() as connection:
        bind_authenticated_tenant(
            connection, session_token=editor_session.session_token,
            organization_id=organization, operation="read",
        )
        latest = connection.execute(GET, _read_args(organization)).one()
        assert latest.checkpoint_id == winner
        assert latest.previous_id == second

    # Distinct transactions racing with the same idempotency key must both
    # converge to the same checkpoint ID, with only one inserted row.
    start = Barrier(2, timeout=12)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_repeat = pool.submit(race, "checkpoint-race-idempotent", winner)
        second_repeat = pool.submit(race, "checkpoint-race-idempotent", winner)
        repeat_outcomes = [first_repeat.result(timeout=30), second_repeat.result(timeout=30)]
    assert all(kind == "created" for kind, _ in repeat_outcomes)
    assert repeat_outcomes[0][1] == repeat_outcomes[1][1]
    idempotent_id = repeat_outcomes[0][1]
    assert isinstance(idempotent_id, uuid.UUID) and idempotent_id != winner

    with clone_owner.connect() as connection:
        with connection.begin_nested():
            try:
                with connection.begin_nested():
                    connection.execute(text("""
                        UPDATE public.a72_canonical_checkpoints
                        SET price_basis='retail' WHERE organization_id=:org
                    """), {"org": organization})
            except DBAPIError:
                pass
            else:
                raise AssertionError("Checkpoint history was mutable.")
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_checkpoints
            WHERE organization_id=:org
        """), {"org": organization}) == 5
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_row_decisions
            WHERE organization_id=:org
        """), {"org": organization}) == 15
        assert connection.scalar(text("""
            SELECT count(*) FROM public.a72_canonical_checkpoints
            WHERE organization_id=:org
        """), {"org": outsider}) == 0
        for signature in (
            "public.a72_checkpoint_put(uuid,text,text,text,text,text,text,uuid,jsonb)",
            "public.a72_checkpoint_get(uuid,text,text,text,text,text)",
        ):
            assert connection.scalar(text("""
                SELECT has_function_privilege(
                    'kpdesk_auth_runtime', :signature, 'EXECUTE'
                )
            """), {"signature": signature}) is False
            assert connection.scalar(text("""
                SELECT has_function_privilege(
                    'kpdesk_tenant_runtime', :signature, 'EXECUTE'
                )
            """), {"signature": signature}) is True
