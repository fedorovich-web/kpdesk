"""A97 proposed migration remains isolated and matches frozen taxonomy."""
from __future__ import annotations

import importlib
import re

import pytest

from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES
from scripts.audit_migration_lineage import audit_migration_lineage

proposal = importlib.import_module("migrations.versions.0031_catalog_taxonomy_v4")
legacy = importlib.import_module("migrations.versions.0003_broad_catalog")


def test_candidate_freezes_exact_v3_to_v4_vocabulary() -> None:
    previous = frozenset(re.findall(r"'([a-z_]+)'", legacy.V3))
    assert previous == frozenset(proposal.V3_CATEGORIES)
    assert len(previous) == 34
    current = frozenset(proposal.V4_CATEGORIES)
    assert current == set(CATEGORY_ATTRIBUTES)
    assert len(current) == 40
    assert sorted(current - previous) == list(proposal.NEW_CATEGORIES)
    assert proposal.revision == "0031_catalog_taxonomy_v4"
    assert proposal.down_revision == "0030_tenant_llm_cache"
    assert proposal.branch_labels is None
    assert proposal.depends_on is None
    assert proposal._predicate(proposal.V4_CATEGORIES).startswith("category IN (")


@pytest.mark.parametrize("unsafe", [
    ("paper", "paper"), ("paper', TRUE OR 'x",), ("paper;DROP",),
    ("",), (),
])
def test_candidate_rejects_duplicate_or_unsafe_category_values(
    unsafe: tuple[str, ...],
) -> None:
    with pytest.raises(RuntimeError, match="frozen category"):
        proposal._predicate(unsafe)


def test_released_local_taxonomy_precedes_pending_supplier_audit_revision() -> None:
    audited = audit_migration_lineage()
    assert audited.release_head == "0049_yandex_id"
    assert proposal.revision in audited.release_revisions
    supplier = importlib.import_module("migrations.versions.0032_supplier_quote_audit")
    assert supplier.down_revision == proposal.revision
