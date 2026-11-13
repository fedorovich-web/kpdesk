"""A34 immutable tenant review of source-verified procurement candidate proposals.

Revision ID: 0025_request_candidate_audit
Revises: 0024_supplier_unit_reviews

Never authorizes original source rights, ML gold or benchmark evaluation.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0025_request_candidate_audit"
down_revision = "0024_supplier_unit_reviews"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    connection = op.get_bind()
    database = connection.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A34 requires KPDesk or marked disposable clone.")
        marker = connection.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if marker != "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("A34 clone provenance mismatch.")
    if connection.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A34 requires schema owner.")
    if connection.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0024_supplier_unit_reviews"
    ):
        raise RuntimeError("A34 expects release 0024.")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.tenant_request_candidate_reviews')"
    )) is not None:
        raise RuntimeError("A34 immutable table already exists.")

    op.create_table(
        "tenant_request_candidate_reviews",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("queue_sha256", sa.String(64), nullable=False),
        sa.Column("task_sha256", sa.String(64), nullable=False),
        sa.Column("intake_sha256", sa.String(64), nullable=False),
        sa.Column("catalog_sha256", sa.String(64), nullable=False),
        sa.Column("split_sha256", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_batch_id", sa.String(100), nullable=False),
        sa.Column("partition", sa.String(10), nullable=False),
        sa.Column("candidate_skus", JSONB(), nullable=False),
        sa.Column("accepted_skus", JSONB(), nullable=False),
        sa.Column("rejected_skus", JSONB(), nullable=False),
        sa.Column("unresolved_skus", JSONB(), nullable=False),
        sa.Column("no_candidate_match", sa.Boolean(), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint(
            "organization_id", "queue_sha256", "task_sha256",
            "reviewer_user_id", name="pk_tenant_candidate_review",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "queue_sha256 ~ '^[a-f0-9]{64}$' "
            "AND task_sha256 ~ '^[a-f0-9]{64}$' "
            "AND intake_sha256 ~ '^[a-f0-9]{64}$' "
            "AND catalog_sha256 ~ '^[a-f0-9]{64}$' "
            "AND split_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_candidate_review_digest",
        ),
        sa.CheckConstraint(
            "request_id ~ '^[A-Za-z0-9_-]{1,100}$' "
            "AND source_batch_id ~ '^[A-Za-z0-9_-]{1,100}$' "
            "AND partition IN ('train','validation')",
            name="ck_candidate_review_request",
        ),
        sa.CheckConstraint(
            "length(trim(rationale)) BETWEEN 32 AND 1000",
            name="ck_candidate_review_reason",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(candidate_skus)='array' "
            "AND jsonb_array_length(candidate_skus) BETWEEN 2 AND 20 "
            "AND jsonb_typeof(accepted_skus)='array' "
            "AND jsonb_typeof(rejected_skus)='array' "
            "AND jsonb_typeof(unresolved_skus)='array'",
            name="ck_candidate_review_json_shape",
        ),
        sa.CheckConstraint(
            "(no_candidate_match AND accepted_skus='[]'::jsonb "
            "AND unresolved_skus='[]'::jsonb "
            "AND rejected_skus=candidate_skus) OR NOT no_candidate_match",
            name="ck_candidate_review_no_match_scope",
        ),
    )
    op.create_index(
        "ix_tenant_candidate_review_queue",
        "tenant_request_candidate_reviews",
        ["organization_id", "queue_sha256", "request_id", "recorded_at"],
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_request_candidate_reviews
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_request_candidate_reviews ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_request_candidate_reviews FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_candidate_review_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_request_candidate_reviews
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_candidate_review_attest(
          p_org uuid, p_queue text, p_task text, p_intake text,
          p_catalog text, p_split text, p_request text, p_source text,
          p_batch text, p_partition text, p_candidates jsonb,
          p_positive jsonb, p_negative jsonb, p_unresolved jsonb,
          p_no_match boolean, p_rationale text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid;
        DECLARE existing boolean;
        BEGIN
          IF p_org IS NULL
             OR p_queue !~ '^[a-f0-9]{64}$'
             OR p_task !~ '^[a-f0-9]{64}$'
             OR p_intake !~ '^[a-f0-9]{64}$'
             OR p_catalog !~ '^[a-f0-9]{64}$'
             OR p_split !~ '^[a-f0-9]{64}$'
             OR p_source !~ '^[a-f0-9]{64}$'
             OR p_request !~ '^[A-Za-z0-9_-]{1,100}$'
             OR p_batch !~ '^[A-Za-z0-9_-]{1,100}$'
             OR p_partition NOT IN ('train','validation')
             OR p_no_match IS NULL OR p_rationale IS NULL
             OR length(trim(p_rationale)) NOT BETWEEN 32 AND 1000
             OR jsonb_typeof(p_candidates) IS DISTINCT FROM 'array'
             OR jsonb_typeof(p_positive) IS DISTINCT FROM 'array'
             OR jsonb_typeof(p_negative) IS DISTINCT FROM 'array'
             OR jsonb_typeof(p_unresolved) IS DISTINCT FROM 'array'
             OR jsonb_array_length(p_candidates) NOT BETWEEN 2 AND 20
          THEN RETURN FALSE; END IF;
          IF EXISTS (
             SELECT 1 FROM jsonb_array_elements(p_candidates) item
             WHERE jsonb_typeof(item)!='string'
                OR length(trim(item #>> '{}')) NOT BETWEEN 1 AND 100
          ) OR EXISTS (
             SELECT 1 FROM (
                SELECT item FROM jsonb_array_elements(p_candidates) item
                GROUP BY item HAVING count(*)>1
             ) duplicates
          ) THEN RETURN FALSE; END IF;
          IF EXISTS (
             SELECT 1 FROM (
                SELECT item FROM (
                  SELECT jsonb_array_elements(p_positive) item
                  UNION ALL SELECT jsonb_array_elements(p_negative)
                  UNION ALL SELECT jsonb_array_elements(p_unresolved)
                ) all_items
                GROUP BY item HAVING count(*)!=1
             ) duplicates
          ) OR (SELECT count(*) FROM (
                 SELECT jsonb_array_elements(p_positive) item
                 UNION ALL SELECT jsonb_array_elements(p_negative)
                 UNION ALL SELECT jsonb_array_elements(p_unresolved)
               ) labels) != jsonb_array_length(p_candidates)
             OR EXISTS (
               SELECT item FROM (
                 SELECT jsonb_array_elements(p_positive) item
                 UNION ALL SELECT jsonb_array_elements(p_negative)
                 UNION ALL SELECT jsonb_array_elements(p_unresolved)
               ) labels
               EXCEPT SELECT jsonb_array_elements(p_candidates)
             )
             OR (p_no_match AND (
               p_positive!='[]'::jsonb OR p_unresolved!='[]'::jsonb
               OR p_negative!=p_candidates
             ))
             OR (NOT p_no_match AND p_positive='[]'::jsonb
                 AND p_unresolved='[]'::jsonb)
          THEN RETURN FALSE; END IF;
          IF current_setting('kpdesk.session_sha256',true)
                !~ '^[a-f0-9]{64}$'
             OR NOT public.kpdesk_session_tenant_allowed(p_org,'write')
          THEN RETURN FALSE; END IF;
          SELECT s.user_id INTO actor FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m
            ON m.organization_id=p_org AND m.user_id=s.user_id
          WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.status='active' AND m.role IN ('owner','editor');
          IF actor IS NULL THEN RETURN FALSE; END IF;
          INSERT INTO public.tenant_request_candidate_reviews(
             organization_id,queue_sha256,task_sha256,intake_sha256,
             catalog_sha256,split_sha256,request_id,source_sha256,
             source_batch_id,partition,candidate_skus,accepted_skus,
             rejected_skus,unresolved_skus,no_candidate_match,
             rationale,reviewer_user_id
          ) VALUES(
             p_org,p_queue,p_task,p_intake,p_catalog,p_split,p_request,p_source,
             p_batch,p_partition,p_candidates,p_positive,p_negative,
             p_unresolved,p_no_match,p_rationale,actor
          ) ON CONFLICT DO NOTHING;
          SELECT EXISTS (
             SELECT 1 FROM public.tenant_request_candidate_reviews r
             WHERE r.organization_id=p_org AND r.queue_sha256=p_queue
               AND r.task_sha256=p_task AND r.reviewer_user_id=actor
               AND r.intake_sha256=p_intake AND r.catalog_sha256=p_catalog
               AND r.split_sha256=p_split AND r.request_id=p_request
               AND r.source_sha256=p_source AND r.source_batch_id=p_batch
               AND r.partition=p_partition AND r.candidate_skus=p_candidates
               AND r.accepted_skus=p_positive AND r.rejected_skus=p_negative
               AND r.unresolved_skus=p_unresolved
               AND r.no_candidate_match=p_no_match AND r.rationale=p_rationale
          ) INTO existing;
          RETURN existing;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_candidate_review_get(
          p_org uuid,p_queue text,p_limit integer,p_offset integer
        ) RETURNS TABLE(
          task_sha256 text,request_id text,source_sha256 text,
          source_batch_id text,partition text,candidate_skus jsonb,
          accepted_skus jsonb,rejected_skus jsonb,unresolved_skus jsonb,
          no_candidate_match boolean,rationale text,reviewer_user_id uuid,
          recorded_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT r.task_sha256::text,r.request_id::text,r.source_sha256::text,
             r.source_batch_id::text,r.partition::text,r.candidate_skus,
             r.accepted_skus,r.rejected_skus,r.unresolved_skus,
             r.no_candidate_match,r.rationale::text,r.reviewer_user_id,
             r.recorded_at
          FROM public.tenant_request_candidate_reviews r
          WHERE p_org IS NOT NULL
            AND p_queue ~ '^[a-f0-9]{64}$'
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
          ORDER BY r.request_id,r.recorded_at,r.reviewer_user_id
          LIMIT p_limit OFFSET p_offset
        $$;
    """)
    for signature in (
        "kpdesk_candidate_review_attest(uuid,text,text,text,text,text,text,text,text,text,jsonb,jsonb,jsonb,jsonb,boolean,text)",
        "kpdesk_candidate_review_get(uuid,text,integer,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("A34 immutable candidate reviews must be retained with backup.")
