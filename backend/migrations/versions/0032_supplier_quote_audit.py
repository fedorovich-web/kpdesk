"""Make manager SKU audit source-version-aware for multi-supplier quotes.

Revision ID: 0032_supplier_quote_audit
Revises: 0031_catalog_taxonomy_v4

This is an explicit gated migration. Do not run it on the working database
during development; only independently provenance-verified disposable clones
may run it automatically in tests.
"""
from __future__ import annotations

import os
import re

import sqlalchemy as sa
from alembic import op

revision = "0032_supplier_quote_audit"
down_revision = "0031_catalog_taxonomy_v4"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")
_FK = "fk_quote_manager_source_tenant_version"


def _preflight() -> sa.Connection:
    conn = op.get_bind()
    name, owner, version = conn.execute(sa.text("""
        SELECT current_database(),current_user,
               (SELECT version_num FROM public.alembic_version)
    """)).one()
    if owner != "kpdesk" or version != down_revision:
        raise RuntimeError("Supplier audit requires exact owner and schema revision 0031.")
    if name == "kpdesk":
        if os.environ.get("KP_DESK_APPROVE_LOCAL_0032") != "1":
            raise RuntimeError("Supplier audit migration on local DB is not authorized.")
        counts = conn.execute(sa.text("""
            SELECT (SELECT count(*) FROM public.organizations),
                   (SELECT count(*) FROM public.users),
                   (SELECT count(*) FROM public.catalog_items),
                   (SELECT count(*) FROM public.quote_documents)
        """)).one()
        if counts != (0, 0, 0, 0):
            raise RuntimeError("Supplier audit requires empty working application tables.")
    elif isinstance(name, str) and _CLONE.fullmatch(name):
        marker = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": name})
        if marker != "kpdesk:accounts-http:" + name.rsplit("_", 1)[-1]:
            raise RuntimeError("Supplier audit requires verified clone provenance.")
    else:
        raise RuntimeError("Supplier audit migration refuses unrecognized database.")
    if conn.scalar(sa.text("""
        SELECT to_regprocedure('public.kpdesk_quote_manager_record()')
    """)) is None:
        raise RuntimeError("Supplier audit cannot find original manager trigger.")
    if conn.scalar(sa.text("""
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.tenant_quote_manager_decisions'::regclass
          AND conname=:name
    """), {"name": _FK}) is not None:
        raise RuntimeError("Supplier audit FK already exists.")
    if conn.scalar(sa.text("""
        SELECT EXISTS(
          SELECT 1
          FROM public.tenant_quote_manager_decisions d
          JOIN public.quote_revisions r
            ON r.organization_id=d.organization_id
           AND r.quote_id=d.quote_id AND r.number=d.revision
          CROSS JOIN LATERAL jsonb_array_elements(r.snapshot->'lines') line
          WHERE (line->>'line_number')::integer=d.line_number
            AND line->>'status'='manager_selected'
            AND line->>'source_catalog_version_id' IS NOT NULL
            AND d.catalog_version_id IS DISTINCT FROM
                (line->>'source_catalog_version_id')::uuid
        )
    """)):
        raise RuntimeError("Historical source-tagged manual decisions require repair audit.")
    conn.execute(sa.text("SET LOCAL lock_timeout='5s'"))
    return conn


def upgrade() -> None:
    _preflight()
    op.create_foreign_key(
        _FK, "tenant_quote_manager_decisions", "catalog_versions",
        ["organization_id", "catalog_version_id"], ["organization_id", "id"],
        source_schema="public", referent_schema="public", ondelete="RESTRICT",
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kpdesk_quote_manager_record()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid;
        BEGIN
          IF jsonb_typeof(NEW.snapshot->'lines') IS DISTINCT FROM 'array'
          THEN RAISE EXCEPTION 'quote_manager_snapshot_invalid'; END IF;
          IF NOT EXISTS(
            SELECT 1 FROM jsonb_array_elements(NEW.snapshot->'lines') line
            WHERE line->>'status'='manager_selected'
          ) THEN RETURN NEW; END IF;
          IF NEW.organization_id IS NULL
            OR coalesce(current_setting('kpdesk.session_sha256',true),'')
               !~ '^[a-f0-9]{64}$'
            OR NOT public.kpdesk_session_tenant_allowed(
                NEW.organization_id,'write'
            )
          THEN RAISE EXCEPTION 'quote_manager_unauthorized'
            USING ERRCODE='42501'; END IF;
          SELECT s.user_id INTO actor FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m
            ON m.user_id=s.user_id AND m.organization_id=NEW.organization_id
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.status='active' AND m.role IN ('owner','editor');
          IF actor IS NULL THEN RAISE EXCEPTION 'quote_manager_unauthorized'
            USING ERRCODE='42501'; END IF;
          IF EXISTS(
            SELECT 1 FROM jsonb_array_elements(NEW.snapshot->'lines') line
            WHERE line->>'status'='manager_selected'
              AND (jsonb_typeof(line->'line_number') IS DISTINCT FROM 'number'
                OR (line->>'line_number')::integer NOT BETWEEN 1 AND 20
                OR jsonb_typeof(line->'sku') IS DISTINCT FROM 'string'
                OR length(trim(line->>'sku')) NOT BETWEEN 1 AND 100
                OR (
                  line->>'source_catalog_version_id' IS NULL
                  AND (
                    line->>'supplier_id' IS NOT NULL
                    OR line->>'source_sha256' IS NOT NULL
                  )
                )
                OR (
                  line->>'source_catalog_version_id' IS NOT NULL
                  AND (
                    coalesce(line->>'source_catalog_version_id','') !~
                      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    OR coalesce(line->>'supplier_id','') !~
                      '^[A-Za-z0-9_.-]{1,120}$'
                    OR coalesce(line->>'source_sha256','') !~ '^[a-f0-9]{64}$'
                  )
                )
              )
          ) THEN RAISE EXCEPTION 'quote_manager_selection_invalid'; END IF;
          INSERT INTO public.tenant_quote_manager_decisions(
            organization_id,quote_id,revision,line_number,
            catalog_version_id,sku,approved_by_user_id
          )
          SELECT NEW.organization_id,NEW.quote_id,NEW.number,
            (line->>'line_number')::integer,
            coalesce(
              (line->>'source_catalog_version_id')::uuid,
              NEW.catalog_version_id
            ),line->>'sku',actor
          FROM jsonb_array_elements(NEW.snapshot->'lines') line
          WHERE line->>'status'='manager_selected';
          RETURN NEW;
        END $$;
    """)
    # CREATE OR REPLACE retains existing SQL-function ACLs; no new privileges.
    # Existing historical quote revisions and decisions are not rewritten.


def downgrade() -> None:
    raise RuntimeError(
        "Supplier source audit is immutable; restore independently verified backup."
    )
