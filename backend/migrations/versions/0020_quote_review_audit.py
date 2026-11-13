"""A21 immutable server-attested review linked to one priced quote revision.

Revision ID: 0020_quote_review_audit
Revises: 0019_tenant_llm_budget
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0020_quote_review_audit"
down_revision = "0019_tenant_llm_budget"
branch_labels = None
depends_on = None
_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    db = conn.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or _CLONE.fullmatch(db) is None:
            raise RuntimeError("A21 only supports KPDesk or marked auth clone.")
        mark = conn.scalar(sa.text("""
          SELECT shobj_description(oid,'pg_database')
          FROM pg_database WHERE datname=:name
        """), {"name": db})
        if mark != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("A21 clone provenance failed.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A21 requires verified PostgreSQL schema owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0019_tenant_llm_budget"
    ):
        raise RuntimeError("A21 requires applied 0019.")
    if conn.scalar(sa.text(
        "SELECT to_regclass('public.tenant_quote_reviews')"
    )) is not None:
        raise RuntimeError("A21 review table unexpectedly exists.")

    op.add_column(
        "tenant_llm_attempts", sa.Column("source_sha256", sa.String(64)),
    )
    op.add_column(
        "tenant_llm_attempts", sa.Column("preview_sha256", sa.String(64)),
    )
    op.create_table(
        "tenant_quote_reviews",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("extraction_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("preview_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_request_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_line_hashes", JSONB(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint("organization_id", "quote_id", "revision"),
        sa.ForeignKeyConstraint(
            ["organization_id", "quote_id", "revision"],
            ["quote_revisions.organization_id", "quote_revisions.quote_id",
             "quote_revisions.number"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["tenant_llm_attempts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision>=1", name="ck_review_revision"),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND "
            "preview_sha256 ~ '^[0-9a-f]{64}$' AND "
            "reviewed_request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_review_digests",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reviewed_line_hashes)='array' AND "
            "jsonb_array_length(reviewed_line_hashes) BETWEEN 1 AND 20",
            name="ck_review_line_count",
        ),
    )
    op.create_index(
        "ix_tenant_reviews_reviewer_time", "tenant_quote_reviews",
        ["reviewer_user_id", "reviewed_at"],
    )
    op.execute("""
        REVOKE ALL ON public.tenant_quote_reviews
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_review_immutable()
        RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        BEGIN RAISE EXCEPTION 'review_audit_immutable'; END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_review_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_quote_reviews
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_llm_preview_seal(
            p_id uuid,p_digest text,p_source text,p_preview text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
          IF p_id IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
             OR p_source !~ '^[0-9a-f]{64}$'
             OR p_preview !~ '^[0-9a-f]{64}$'
          THEN RETURN FALSE; END IF;
          UPDATE public.tenant_llm_attempts a SET
            source_sha256=p_source,preview_sha256=p_preview
          WHERE a.id=p_id AND a.status='success'
            AND a.preview_sha256 IS NULL
            AND a.created_at>clock_timestamp()-interval '1 hour'
            AND EXISTS(
              SELECT 1 FROM public.auth_sessions s
              WHERE s.token_sha256=p_digest AND s.user_id=a.user_id
            );
          RETURN FOUND;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_quote_review_attest(
          p_org uuid,p_quote uuid,p_revision integer,p_preview uuid,
          p_source_hash text,p_preview_hash text,p_request_hash text,
          p_line_hashes jsonb
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
          actor uuid;
          existing record;
          row_count integer;
        BEGIN
          IF p_org IS NULL OR p_quote IS NULL OR p_preview IS NULL
             OR p_revision IS NULL OR p_revision<1
             OR p_source_hash !~ '^[0-9a-f]{64}$'
             OR p_preview_hash !~ '^[0-9a-f]{64}$'
             OR p_request_hash !~ '^[0-9a-f]{64}$'
             OR jsonb_typeof(p_line_hashes) IS DISTINCT FROM 'array'
             OR jsonb_array_length(p_line_hashes) NOT BETWEEN 1 AND 20
          THEN RETURN FALSE; END IF;
          IF EXISTS(
            SELECT 1 FROM jsonb_array_elements_text(p_line_hashes) val
            WHERE val !~ '^[0-9a-f]{64}$'
          ) THEN RETURN FALSE; END IF;
          IF NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN FALSE; END IF;
          SELECT s.user_id INTO actor
          FROM public.auth_sessions s JOIN public.users u ON u.id=s.user_id
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL;
          IF actor IS NULL THEN RETURN FALSE; END IF;
          IF NOT EXISTS(
            SELECT 1 FROM public.tenant_llm_attempts a
            WHERE a.id=p_preview AND a.organization_id=p_org
              AND a.user_id=actor AND a.status='success'
              AND a.source_sha256=p_source_hash
              AND a.preview_sha256=p_preview_hash
              AND a.created_at>clock_timestamp()-interval '1 hour'
          ) THEN RETURN FALSE; END IF;
          IF NOT EXISTS(
            SELECT 1 FROM public.tenant_quote_write_requests w
            WHERE w.organization_id=p_org AND w.quote_id=p_quote
              AND w.revision_number=p_revision
              AND w.request_sha256=p_request_hash
          ) THEN RETURN FALSE; END IF;
          SELECT jsonb_array_length(r.snapshot->'lines') INTO row_count
          FROM public.quote_revisions r
          WHERE r.organization_id=p_org AND r.quote_id=p_quote
            AND r.number=p_revision;
          IF row_count IS NULL OR row_count<>jsonb_array_length(p_line_hashes)
          THEN RETURN FALSE; END IF;
          INSERT INTO public.tenant_quote_reviews(
             organization_id,quote_id,revision,extraction_id,reviewer_user_id,
             source_sha256,preview_sha256,reviewed_request_sha256,
             reviewed_line_hashes
          ) VALUES(
             p_org,p_quote,p_revision,p_preview,actor,p_source_hash,
             p_preview_hash,p_request_hash,p_line_hashes
          ) ON CONFLICT DO NOTHING;
          SELECT * INTO existing FROM public.tenant_quote_reviews
          WHERE organization_id=p_org AND quote_id=p_quote
             AND revision=p_revision;
          RETURN FOUND AND existing.extraction_id=p_preview
            AND existing.reviewer_user_id=actor
            AND existing.source_sha256=p_source_hash
            AND existing.preview_sha256=p_preview_hash
            AND existing.reviewed_request_sha256=p_request_hash
            AND existing.reviewed_line_hashes=p_line_hashes;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_quote_review_get(
          p_org uuid,p_quote uuid,p_revision integer
        ) RETURNS TABLE(
          reviewer_user_id uuid,reviewed_at timestamptz,
          extraction_id uuid,source_sha256 text,preview_sha256 text,
          reviewed_request_sha256 text,reviewed_line_hashes jsonb
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.reviewer_user_id,r.reviewed_at,r.extraction_id,
                 r.source_sha256::text,r.preview_sha256::text,
                 r.reviewed_request_sha256::text,r.reviewed_line_hashes
          FROM public.tenant_quote_reviews r
          WHERE r.organization_id=p_org AND r.quote_id=p_quote
            AND r.revision=p_revision
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
        $$;
    """)
    for signature in (
        "kpdesk_llm_preview_seal(uuid,text,text,text)",
        "kpdesk_quote_review_attest(uuid,uuid,integer,uuid,text,text,text,jsonb)",
        "kpdesk_quote_review_get(uuid,uuid,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} "
                   "TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("Immutable audit requires reviewed data retention rollback.")
