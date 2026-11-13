"""Canonical inventory of reviewed-catalog SQL objects (A93)."""
from __future__ import annotations

REVIEW_TABLES = (
    "a79_catalog_review_provenance",
    "a77_current_row",
    "a77_current_head",
    "a72_canonical_row_decisions",
    "a72_canonical_checkpoints",
    "a84_provenance_signing_secret",
)

REVIEW_FUNCTIONS = (
    "a79_bind_published_review(uuid,uuid,uuid,text,text,text,text,text,text,uuid,bigint,text)",
    "a79_review_snapshot(uuid,text,text,text,text,text,uuid)",
    "a77_checkpoint_probe(uuid,text,text,text,text,text,integer[])",
    "a77_checkpoint_page(uuid,text,text,text,text,text,integer,integer)",
    "a77_apply_immutable_decisions()",
    "a76_checkpoint_probe(uuid,text,text,text,text,text,integer[])",
    "a75_checkpoint_page(uuid,text,text,text,text,text,integer,integer)",
    "a72_checkpoint_get(uuid,text,text,text,text,text)",
    "a72_checkpoint_put(uuid,text,text,text,text,text,text,uuid,jsonb)",
)

# Read-only audit drift expected while every tenant-facing review entrypoint
# is deliberately disabled. The immutable-decisions trigger writer stays denied.
REVIEW_INACTIVE_ISSUES = frozenset(
    "function_grant_mismatch:kpdesk_tenant_runtime:" + name.split("(", 1)[0]
    for name in REVIEW_FUNCTIONS
    if name != "a77_apply_immutable_decisions()"
)
