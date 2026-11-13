"""A43: immutable, session-attested manual SKU decisions on quote revisions.

Revision ID: 0028_quote_manager_decisions
Revises: 0027_candidate_adjudications
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0028_quote_manager_decisions"
down_revision = "0027_candidate_adjudications"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    database = connection.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("Manager decision migration requires KPDesk or marked clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:database
        """), {"database": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("Manager decision clone provenance mismatch.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("Manager decision migration requires schema owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0027_candidate_adjudications"
    ):
        raise RuntimeError("Manager decision migration requires exact revision 0027.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_quote_manager_decisions')"
    )) is not None:
        raise RuntimeError("Manager decision table already exists.")
    if connection.scalar(sa.text(
        "SELECT to_regprocedure('public.kpdesk_quote_manager_record()')"
    )) is not None:
        raise RuntimeError("Manager decision trigger function already exists.")
    if connection.scalar(sa.text("""
        SELECT EXISTS(
          SELECT 1 FROM public.quote_revisions r
          WHERE r.snapshot @> '{"lines":[{"status":"manager_selected"}]}'::jsonb
        )
    """)):
        raise RuntimeError(
            "Existing manual approvals require provenance review before migration."
        )

    op.create_table(
        "tenant_quote_manager_decisions",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("catalog_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(100), nullable=False),
        sa.Column("approved_by_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "quote_id", "revision", "line_number",
            name="pk_quote_manager_decision",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_id", "revision"],
            ["quote_revisions.organization_id", "quote_revisions.quote_id",
             "quote_revisions.number"],
            name="fk_quote_manager_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id", "sku"], ["catalog_items.catalog_version_id",
                                           "catalog_items.sku"],
            name="fk_quote_manager_catalog_sku", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"],
            name="fk_quote_manager_actor", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "revision>=1 AND line_number BETWEEN 1 AND 20 "
            "AND length(trim(sku)) BETWEEN 1 AND 100",
            name="ck_quote_manager_decision_fields",
        ),
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_quote_manager_decisions
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_quote_manager_decisions
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_quote_manager_decisions
        FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_quote_manager_decision_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_quote_manager_decisions
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_quote_manager_record()
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
                OR length(trim(line->>'sku')) NOT BETWEEN 1 AND 100)
          ) THEN RAISE EXCEPTION 'quote_manager_selection_invalid'; END IF;
          INSERT INTO public.tenant_quote_manager_decisions(
            organization_id,quote_id,revision,line_number,
            catalog_version_id,sku,approved_by_user_id
          )
          SELECT NEW.organization_id,NEW.quote_id,NEW.number,
            (line->>'line_number')::integer,
            NEW.catalog_version_id,line->>'sku',actor
          FROM jsonb_array_elements(NEW.snapshot->'lines') line
          WHERE line->>'status'='manager_selected';
          RETURN NEW;
        END $$;
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.kpdesk_quote_manager_record()
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        CREATE TRIGGER trg_quote_revision_manager_record
        AFTER INSERT ON public.quote_revisions
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_quote_manager_record()
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Immutable manager decisions require reviewed, lossless migration rollback."
    )
