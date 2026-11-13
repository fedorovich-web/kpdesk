"""A92 read-only PostgreSQL readiness audit for reviewed catalog rollout.

No DDL, no grants, no secrets returned. A passing audit is necessary but NOT
sufficient for a production rollout: worker key distribution and approvals are
separate, explicit release blockers.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

_TABLES = (
    "a72_canonical_checkpoints", "a72_canonical_row_decisions",
    "a77_current_head", "a77_current_row",
    "a79_catalog_review_provenance", "a84_provenance_signing_secret",
)
_FUNCTIONS = (
    "a72_checkpoint_put", "a72_checkpoint_get",
    "a75_checkpoint_page", "a76_checkpoint_probe",
    "a77_apply_immutable_decisions", "a77_checkpoint_page",
    "a77_checkpoint_probe", "a79_review_snapshot",
    "a79_bind_published_review",
)
_ROLES = ("kpdesk_app", "kpdesk_auth_runtime", "kpdesk_tenant_runtime")
_EXPECTED_INDEX = {
    "a72_canonical_checkpoints": (
        ("organization_id", "supplier_id", "source_sha256",
         "mapping_sha256", "taxonomy_version", "price_basis",
         "created_at desc", "checkpoint_id desc"),
        ("organization_id", "supplier_id", "source_sha256",
         "mapping_sha256", "idempotency_key"),
    ),
    "a72_canonical_row_decisions": (("checkpoint_id", "source_line"),),
    "a77_current_head": (
        ("organization_id", "supplier_id", "source_sha256",
         "mapping_sha256", "taxonomy_version", "price_basis"),
    ),
    "a77_current_row": (
        ("organization_id", "supplier_id", "source_sha256",
         "mapping_sha256", "taxonomy_version", "price_basis", "source_line"),
    ),
    "a79_catalog_review_provenance": (("organization_id", "catalog_version_id"),),
}


@dataclass(frozen=True, slots=True)
class ReviewSchemaAudit:
    revision: str | None
    signing_epoch: int | None
    issues: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.issues

    def require_ready(self) -> None:
        if self.issues:
            raise RuntimeError("Reviewed catalog schema is not ready: " + ", ".join(self.issues))


def audit_review_schema(
    connection: Connection, *, expected_revision: str,
) -> ReviewSchemaAudit:
    """Query catalog metadata only. Does not retrieve or modify signing secrets."""
    if expected_revision not in (
        "0030_tenant_llm_cache", "0031_reviewed_catalog_clone",
        "0031_review_schema_candidate",
    ):
        raise ValueError("Unrecognized reviewed-catalog audit revision.")
    issues: list[str] = []
    revision = connection.scalar(text(
        "SELECT version_num FROM public.alembic_version"
    ))
    if revision != expected_revision:
        issues.append("alembic_revision_mismatch")

    tables = connection.execute(text("""
        SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND c.relname=ANY(:names)
    """), {"names": list(_TABLES)}).all()
    present = {row.relname: row for row in tables}
    for name in _TABLES:
        row = present.get(name)
        if row is None:
            issues.append("missing_table:" + name)
        elif not row.relrowsecurity or not row.relforcerowsecurity:
            issues.append("rls_not_forced:" + name)

    policies = connection.execute(text("""
        SELECT tablename,policyname,roles,cmd
        FROM pg_catalog.pg_policies
        WHERE schemaname='public' AND tablename=ANY(:names)
    """), {"names": list(_TABLES)}).all()
    for name in _TABLES:
        if name not in present:
            continue
        table_policies = [p for p in policies if p.tablename == name]
        if len(table_policies) != 1 or table_policies[0].roles != ["kpdesk"]:
            issues.append("unsafe_table_policy:" + name)

    role_flags = connection.execute(text("""
        SELECT rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb
        FROM pg_catalog.pg_roles WHERE rolname=ANY(:roles)
    """), {"roles": list(_ROLES)}).all()
    if len(role_flags) != len(_ROLES):
        issues.append("missing_runtime_role")
    for role in role_flags:
        if role.rolsuper or role.rolbypassrls or role.rolcreaterole or role.rolcreatedb:
            issues.append("unsafe_role_privilege:" + role.rolname)
        # No runtime role may become the schema owner or create arbitrary SQL.
        role_privilege = connection.execute(text("""
            SELECT pg_has_role(:role,'kpdesk','MEMBER') AS owner_member,
                   has_schema_privilege(:role,'public','CREATE') AS schema_create
        """), {"role": role.rolname}).one()
        if role_privilege.owner_member or role_privilege.schema_create:
            issues.append("runtime_can_escalate:" + role.rolname)
        for name in present:
            for right in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE",
                          "REFERENCES", "TRIGGER"):
                has_access = connection.scalar(text("""
                    SELECT has_table_privilege(:role, :relation, :permission)
                """), {
                    "role": role.rolname,
                    "relation": "public." + name,
                    "permission": right,
                })
                if has_access:
                    issues.append("direct_table_access:" + role.rolname + ":" + name)
                    break

    functions = connection.execute(text("""
        SELECT p.proname,p.oid,p.prosecdef,p.proconfig,
               EXISTS (
                 SELECT 1 FROM aclexplode(
                   coalesce(p.proacl,acldefault('f',p.proowner))
                 ) acl WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
               ) AS public_can_execute
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname=ANY(:names)
    """), {"names": list(_FUNCTIONS)}).all()
    function_index = {row.proname: row for row in functions}
    for name in _FUNCTIONS:
        row = function_index.get(name)
        if row is None:
            issues.append("missing_function:" + name)
            continue
        if not row.prosecdef or "search_path=pg_catalog,pg_temp" not in (
            option.replace(" ", "") for option in (row.proconfig or ())
        ):
            issues.append("unsafe_security_definer:" + name)
        if row.public_can_execute:
            issues.append("public_function_execute:" + name)
        for runtime_role in _ROLES:
            permitted = connection.scalar(text("""
                SELECT has_function_privilege(:role,:func_oid,'EXECUTE')
            """), {"role": runtime_role, "func_oid": row.oid})
            expected = runtime_role == "kpdesk_tenant_runtime" and (
                name != "a77_apply_immutable_decisions"
            )
            if permitted is not expected:
                issues.append("function_grant_mismatch:" + runtime_role + ":" + name)

    if len(functions) != len(_FUNCTIONS):
        issues.append("unexpected_function_overload")
    indexed = connection.execute(text("""
        SELECT tablename,indexdef FROM pg_catalog.pg_indexes
        WHERE schemaname='public' AND tablename=ANY(:names)
    """), {"names": list(_TABLES)}).all()
    by_table: dict[str, list[str]] = {}
    for row in indexed:
        by_table.setdefault(row.tablename, []).append(
            " ".join(row.indexdef.lower().split())
        )
    for table, patterns in _EXPECTED_INDEX.items():
        for group in patterns:
            if not any(all(column in idx for column in group)
                       for idx in by_table.get(table, ())):
                issues.append("missing_composite_index:" + table + ":" + group[-1])

    triggers = connection.execute(text("""
        SELECT c.relname,t.tgname,t.tgenabled
        FROM pg_catalog.pg_trigger t
        JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal
          AND c.relname=ANY(:names)
    """), {"names": list(_TABLES)}).all()
    enabled = {(item.relname, item.tgname) for item in triggers
               if item.tgenabled in ("O", "A")}
    expected_triggers = (
        ("a72_canonical_checkpoints", "a72_a72_canonical_checkpoints_immutable"),
        ("a72_canonical_row_decisions", "a72_a72_canonical_row_decisions_immutable"),
        ("a72_canonical_row_decisions", "a77_materialize_decisions"),
        ("a79_catalog_review_provenance", "a79_provenance_immutable"),
        ("a84_provenance_signing_secret", "a84_signing_secret_immutable"),
    )
    for table, name in expected_triggers:
        if (table, name) not in enabled:
            issues.append("missing_immutable_trigger:" + table + ":" + name)

    key_epoch: int | None = None
    if "a84_provenance_signing_secret" in present:
        key_status = connection.execute(text("""
            SELECT count(*) AS key_count, max(key_epoch) AS epoch,
                   bool_and(octet_length(secret)=32) AS correct_length
            FROM public.a84_provenance_signing_secret
        """)).one()
        if key_status.key_count < 1 or key_status.correct_length is not True:
            issues.append("invalid_signer_metadata")
        else:
            key_epoch = key_status.epoch

    return ReviewSchemaAudit(
        revision=revision, signing_epoch=key_epoch,
        issues=tuple(sorted(set(issues))),
    )
