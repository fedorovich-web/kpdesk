"""Pure A12 release-preflight safety guards; no production DB connection."""
from __future__ import annotations

import pytest

from scripts.rehearse_release_snapshot import (
    _canonical_constraint,
    _require_empty_sensitive_sources,
    _require_generated,
)


def test_clone_identifier_must_be_nonce_marked() -> None:
    _require_generated("kpdesk_release_e2e_012345abcdef")
    for unsafe in (
        "kpdesk", "kpdesk_tenant_test", "postgres",
        "kpdesk_release_e2e_01234", "kpdesk_release_e2e_012345abcdef;DROP",
        "kpdesk_release_e2e_012345ABCDEF",
    ):
        with pytest.raises(RuntimeError, match="unrecognized"):
            _require_generated(unsafe)


def test_unencrypted_rehearsal_refuses_existing_customers_or_quotes() -> None:
    empty = {
        "catalog_versions": 0, "catalog_items": 0, "idempotency_records": 0,
        "quote_documents": 0, "quote_revisions": 0, "quote_write_requests": 0,
        "organizations": 0, "users": 0, "organization_memberships": 0,
    }
    _require_empty_sensitive_sources(empty)
    for table in ("users", "catalog_items", "quote_revisions"):
        populated = {**empty, table: 1}
        with pytest.raises(RuntimeError, match="unencrypted backup is forbidden"):
            _require_empty_sensitive_sources(populated)


def test_pg17_enum_check_is_same_semantics_after_pg_restore() -> None:
    original = (
        "CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, "
        "'editor'::character varying, 'viewer'::character varying])::text[])))"
    )
    restored = (
        "CHECK (((role)::text = ANY (ARRAY[('owner'::character varying)::text, "
        "('editor'::character varying)::text, ('viewer'::character varying)::text])))"
    )
    left = _canonical_constraint("organization_memberships", "ck_membership_role", "c",
                                 original)
    right = _canonical_constraint("organization_memberships", "ck_membership_role", "c",
                                  restored)
    assert left == right
    assert left[-1] == "CHECK_ENUM(role,owner,editor,viewer)"


@pytest.mark.parametrize("tampering", [
    (
        "CHECK (((role)::text = ANY (ARRAY[('owner'::character varying)::text, "
        "('editor'::character varying)::text])))"
    ),
    (
        "CHECK (((role)::text = ANY (ARRAY[('viewer'::character varying)::text, "
        "('editor'::character varying)::text, ('owner'::character varying)::text])))"
    ),
    (
        "CHECK (((role)::text = ANY (ARRAY[('owner'::character varying)::text, "
        "('editor'::character varying)::text, ('viewer'::character varying)::text]))) OR TRUE"
    ),
])
def test_pg17_constraint_normalization_rejects_modified_role_policy(
    tampering: str,
) -> None:
    with pytest.raises(RuntimeError, match="CHECK"):
        _canonical_constraint("organization_memberships", "ck_membership_role", "c",
                              tampering)


def test_constraint_name_or_type_is_not_silently_rewritten() -> None:
    definition = "CHECK (price_rub >= 0)"
    assert _canonical_constraint("catalog_items", "nonnegative_price", "c",
                                 definition) == (
        "catalog_items", "nonnegative_price", "c", definition,
    )
    with pytest.raises(RuntimeError, match="changed type"):
        _canonical_constraint("users", "ck_user_status", "f", definition)
