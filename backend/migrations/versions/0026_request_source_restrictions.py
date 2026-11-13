"""A35: append-only source-use restriction attestations; never permissions grants.

Revision ID: 0026_request_source_restrictions
Revises: 0025_request_candidate_audit
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0026_request_source_restrictions"
down_revision = "0025_request_candidate_audit"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[a-f0-9]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    db = connection.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or _CLONE.fullmatch(db) is None:
            raise RuntimeError("A35 requires KPDesk or disposable marked clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": db})
        if marker != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("A35 clone marker invalid.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A35 migration requires the schema owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0025_request_candidate_audit"
    ):
        raise RuntimeError("A35 expects exactly revision 0025.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_request_source_restrictions')"
    )) is not None:
        raise RuntimeError("A35 audit table already exists.")

    op.create_table(
        "tenant_request_source_restrictions",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("queue_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("review_kind", sa.String(16), nullable=False),
        sa.Column("finding", sa.String(24), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=True),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "queue_sha256", "source_sha256",
            "review_kind", "reviewer_user_id",
            name="pk_tenant_request_source_restriction",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "queue_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_sha256 ~ '^[a-f0-9]{64}$' "
            "AND (evidence_sha256 IS NULL OR evidence_sha256 ~ '^[a-f0-9]{64}$')",
            name="ck_request_restriction_hashes",
        ),
        sa.CheckConstraint(
            "review_kind IN ('rights','privacy') "
            "AND finding IN ('requires_evidence','restricted','rejected') "
            "AND length(trim(rationale)) BETWEEN 32 AND 1000",
            name="ck_request_restriction_disposition",
        ),
    )
    op.create_index(
        "ix_tenant_request_source_restrictions_queue",
        "tenant_request_source_restrictions",
        ["organization_id", "queue_sha256", "source_sha256"],
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_request_source_restrictions
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_request_source_restrictions
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_request_source_restrictions
        FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_request_restriction_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_request_source_restrictions
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_request_restriction_attest(
          p_org uuid,p_queue text,p_source text,p_kind text,
          p_finding text,p_evidence text,p_reason text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid;
        DECLARE matched boolean;
        BEGIN
          IF p_org IS NULL OR p_queue IS NULL OR p_source IS NULL
             OR p_queue !~ '^[a-f0-9]{64}$'
             OR p_source !~ '^[a-f0-9]{64}$'
             OR p_kind IS NULL OR p_kind NOT IN ('rights','privacy')
             OR p_finding IS NULL
             OR p_finding NOT IN ('requires_evidence','restricted','rejected')
             OR (p_evidence IS NOT NULL AND p_evidence !~ '^[a-f0-9]{64}$')
             OR p_reason IS NULL
             OR length(trim(p_reason)) NOT BETWEEN 32 AND 1000
             OR coalesce(current_setting('kpdesk.session_sha256',true),'')
                !~ '^[a-f0-9]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN FALSE; END IF;
          SELECT s.user_id INTO actor FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m
            ON m.user_id=s.user_id AND m.organization_id=p_org
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.status='active' AND m.role IN ('owner','editor');
          IF actor IS NULL THEN RETURN FALSE; END IF;
          INSERT INTO public.tenant_request_source_restrictions(
            organization_id,queue_sha256,source_sha256,review_kind,
            finding,evidence_sha256,rationale,reviewer_user_id
          ) VALUES(p_org,p_queue,p_source,p_kind,p_finding,
                   p_evidence,p_reason,actor)
          ON CONFLICT DO NOTHING;
          SELECT EXISTS(
            SELECT 1 FROM public.tenant_request_source_restrictions r
            WHERE r.organization_id=p_org AND r.queue_sha256=p_queue
              AND r.source_sha256=p_source AND r.review_kind=p_kind
              AND r.reviewer_user_id=actor AND r.finding=p_finding
              AND r.evidence_sha256 IS NOT DISTINCT FROM p_evidence
              AND r.rationale=p_reason
          ) INTO matched;
          RETURN matched;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_request_restriction_get(
          p_org uuid,p_queue text,p_limit integer,p_offset integer
        ) RETURNS TABLE(
          source_sha256 text,review_kind text,finding text,
          evidence_sha256 text,rationale text,reviewer_user_id uuid,
          recorded_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.source_sha256::text,r.review_kind::text,r.finding::text,
                 r.evidence_sha256::text,r.rationale::text,
                 r.reviewer_user_id,r.recorded_at
          FROM public.tenant_request_source_restrictions r
          WHERE p_org IS NOT NULL AND p_queue ~ '^[a-f0-9]{64}$'
            AND p_limit BETWEEN 1 AND 100 AND p_offset BETWEEN 0 AND 5000
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
            AND EXISTS (
              SELECT 1 FROM public.auth_sessions s
              JOIN public.organization_memberships m
                ON m.organization_id=p_org AND m.user_id=s.user_id
              WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
                AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
                AND m.status='active' AND m.role IN ('owner','editor')
            )
            AND r.organization_id=p_org AND r.queue_sha256=p_queue
          ORDER BY r.source_sha256,r.review_kind,r.recorded_at,r.reviewer_user_id
          LIMIT p_limit OFFSET p_offset
        $$;
    """)
    for signature in (
        "kpdesk_request_restriction_attest(uuid,text,text,text,text,text,text)",
        "kpdesk_request_restriction_get(uuid,text,integer,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("Source restriction audit is immutable; preserve records.")
