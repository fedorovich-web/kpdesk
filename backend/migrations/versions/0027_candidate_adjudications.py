"""A36: owner-only independent append-only candidate conflict adjudication.

Revision ID: 0027_candidate_adjudications
Revises: 0026_request_source_restrictions
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0027_candidate_adjudications"
down_revision = "0026_request_source_restrictions"
branch_labels = None
depends_on = None
_CLONE = re.compile(r"kpdesk_accounts_http_[a-f0-9]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    db = conn.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or _CLONE.fullmatch(db) is None:
            raise RuntimeError("A36 requires KPDesk or marked disposable clone.")
        marker = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": db})
        if marker != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("A36 clone provenance mismatch.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A36 requires schema owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0026_request_source_restrictions"
    ):
        raise RuntimeError("A36 requires exact revision 0026.")
    if conn.scalar(sa.text(
        "SELECT to_regclass('public.tenant_request_candidate_adjudications')"
    )) is not None:
        raise RuntimeError("A36 adjudication table already exists.")

    op.create_table(
        "tenant_request_candidate_adjudications",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("queue_sha256", sa.String(64), nullable=False),
        sa.Column("task_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("candidate_skus", JSONB(), nullable=False),
        sa.Column("finding", sa.String(28), nullable=False),
        sa.Column("accepted_skus", JSONB(), nullable=False),
        sa.Column("rejected_skus", JSONB(), nullable=False),
        sa.Column("no_candidate_match", sa.Boolean(), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("adjudicator_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("original_reviewer_count", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint(
            "organization_id", "queue_sha256", "task_sha256",
            name="pk_tenant_candidate_adjudication",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adjudicator_user_id"], ["users.id"], ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "queue_sha256 ~ '^[a-f0-9]{64}$' "
            "AND task_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_sha256 ~ '^[a-f0-9]{64}$' "
            "AND request_id ~ '^[A-Za-z0-9_-]{1,100}$'",
            name="ck_candidate_adjudication_identity",
        ),
        sa.CheckConstraint(
            "finding IN ('resolve_proposals','needs_evidence','reject_case') "
            "AND length(trim(rationale)) BETWEEN 32 AND 1000 "
            "AND original_reviewer_count>=2",
            name="ck_candidate_adjudication_finding",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(candidate_skus)='array' "
            "AND jsonb_array_length(candidate_skus) BETWEEN 2 AND 20 "
            "AND jsonb_typeof(accepted_skus)='array' "
            "AND jsonb_typeof(rejected_skus)='array'",
            name="ck_candidate_adjudication_json",
        ),
        sa.CheckConstraint(
            "(finding='resolve_proposals') OR "
            "(accepted_skus='[]'::jsonb AND rejected_skus='[]'::jsonb "
            "AND no_candidate_match=false)",
            name="ck_candidate_adjudication_blocked",
        ),
    )
    op.create_index(
        "ix_tenant_candidate_adjudication_queue",
        "tenant_request_candidate_adjudications",
        ["organization_id", "queue_sha256", "request_id"],
    )
    op.execute("""
        REVOKE ALL ON TABLE public.tenant_request_candidate_adjudications
        FROM PUBLIC,kpdesk_app,kpdesk_auth_runtime,kpdesk_tenant_runtime
    """)
    op.execute("""
        ALTER TABLE public.tenant_request_candidate_adjudications
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.tenant_request_candidate_adjudications
        FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_tenant_candidate_adjudication_immutable
        BEFORE UPDATE OR DELETE ON public.tenant_request_candidate_adjudications
        FOR EACH ROW EXECUTE FUNCTION public.kpdesk_review_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_candidate_adjudicate(
          p_org uuid, p_queue text, p_task text, p_source text,
          p_request text, p_candidates jsonb, p_finding text,
          p_accepted jsonb, p_rejected jsonb, p_no_match boolean,
          p_rationale text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid;
        DECLARE review_count integer;
        DECLARE variants integer;
        DECLARE unresolved boolean;
        DECLARE sample_candidates jsonb;
        DECLARE matched boolean;
        BEGIN
          IF p_org IS NULL OR p_queue IS NULL OR p_task IS NULL
             OR p_source IS NULL OR p_request IS NULL
             OR p_queue !~ '^[a-f0-9]{64}$'
             OR p_task !~ '^[a-f0-9]{64}$'
             OR p_source !~ '^[a-f0-9]{64}$'
             OR p_request !~ '^[A-Za-z0-9_-]{1,100}$'
             OR p_finding IS NULL OR p_finding NOT IN (
                 'resolve_proposals','needs_evidence','reject_case'
             )
             OR p_no_match IS NULL OR p_rationale IS NULL
             OR length(trim(p_rationale)) NOT BETWEEN 32 AND 1000
             OR jsonb_typeof(p_candidates) IS DISTINCT FROM 'array'
             OR jsonb_typeof(p_accepted) IS DISTINCT FROM 'array'
             OR jsonb_typeof(p_rejected) IS DISTINCT FROM 'array'
             OR jsonb_array_length(p_candidates) NOT BETWEEN 2 AND 20
          THEN RETURN FALSE; END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_candidates) value
            WHERE jsonb_typeof(value)!='string'
                OR length(trim(value #>> '{}')) NOT BETWEEN 1 AND 100
          ) OR EXISTS (
            SELECT 1 FROM (
              SELECT value FROM jsonb_array_elements(p_candidates) value
              GROUP BY value HAVING count(*)>1
            ) repeated
          ) THEN RETURN FALSE; END IF;
          IF p_finding='resolve_proposals' THEN
            IF EXISTS (
              SELECT 1 FROM (
                SELECT value FROM jsonb_array_elements(p_accepted) value
                UNION ALL SELECT jsonb_array_elements(p_rejected)
              ) all_values GROUP BY value HAVING count(*)!=1
            ) OR (SELECT count(*) FROM (
              SELECT jsonb_array_elements(p_accepted) value
              UNION ALL SELECT jsonb_array_elements(p_rejected)
            ) full_set) != jsonb_array_length(p_candidates)
              OR EXISTS (
                SELECT value FROM (
                  SELECT jsonb_array_elements(p_accepted) value
                  UNION ALL SELECT jsonb_array_elements(p_rejected)
                ) chosen
                EXCEPT SELECT jsonb_array_elements(p_candidates)
              )
              OR (p_no_match AND (
                p_accepted!='[]'::jsonb OR p_rejected!=p_candidates
              ))
              OR (NOT p_no_match AND p_accepted='[]'::jsonb)
            THEN RETURN FALSE; END IF;
          ELSIF p_accepted!='[]'::jsonb OR p_rejected!='[]'::jsonb
             OR p_no_match THEN RETURN FALSE;
          END IF;
          IF coalesce(current_setting('kpdesk.session_sha256',true),'')
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
            AND m.status='active' AND m.role='owner';
          IF actor IS NULL THEN RETURN FALSE; END IF;
          -- These records were created using real authenticated reviewer IDs.
          -- Source identity, SKU set and request identity must all match.
          SELECT count(*)::integer,
                 count(DISTINCT (
                   r.accepted_skus, r.rejected_skus,
                   r.unresolved_skus, r.no_candidate_match
                 ))::integer,
                 coalesce(bool_or(r.unresolved_skus!='[]'::jsonb),false),
                 min(r.candidate_skus::text)::jsonb
          INTO review_count, variants, unresolved, sample_candidates
          FROM public.tenant_request_candidate_reviews r
          WHERE r.organization_id=p_org AND r.queue_sha256=p_queue
            AND r.task_sha256=p_task AND r.source_sha256=p_source
            AND r.request_id=p_request AND r.candidate_skus=p_candidates;
          IF review_count < 2 OR (variants < 2 AND NOT unresolved)
             OR sample_candidates IS DISTINCT FROM p_candidates
             OR EXISTS (
               SELECT 1 FROM public.tenant_request_candidate_reviews r
               WHERE r.organization_id=p_org AND r.queue_sha256=p_queue
                 AND r.task_sha256=p_task
                 AND (r.source_sha256!=p_source OR r.request_id!=p_request
                      OR r.candidate_skus!=p_candidates)
             ) OR EXISTS (
               SELECT 1 FROM public.tenant_request_candidate_reviews r
               WHERE r.organization_id=p_org AND r.queue_sha256=p_queue
                 AND r.task_sha256=p_task AND r.reviewer_user_id=actor
             )
          THEN RETURN FALSE; END IF;
          INSERT INTO public.tenant_request_candidate_adjudications(
             organization_id,queue_sha256,task_sha256,source_sha256,request_id,
             candidate_skus,finding,accepted_skus,rejected_skus,no_candidate_match,
             rationale,adjudicator_user_id,original_reviewer_count
          ) VALUES(
             p_org,p_queue,p_task,p_source,p_request,p_candidates,p_finding,
             p_accepted,p_rejected,p_no_match,p_rationale,actor,review_count
          ) ON CONFLICT DO NOTHING;
          SELECT EXISTS(
            SELECT 1 FROM public.tenant_request_candidate_adjudications a
            WHERE a.organization_id=p_org AND a.queue_sha256=p_queue
              AND a.task_sha256=p_task AND a.source_sha256=p_source
              AND a.request_id=p_request AND a.candidate_skus=p_candidates
              AND a.finding=p_finding AND a.accepted_skus=p_accepted
              AND a.rejected_skus=p_rejected AND a.no_candidate_match=p_no_match
              AND a.rationale=p_rationale AND a.adjudicator_user_id=actor
          ) INTO matched;
          RETURN matched;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_candidate_adjudications_get(
          p_org uuid,p_queue text,p_limit integer,p_offset integer
        ) RETURNS TABLE(
          task_sha256 text,source_sha256 text,request_id text,
          candidate_skus jsonb,finding text,accepted_skus jsonb,
          rejected_skus jsonb,no_candidate_match boolean,
          rationale text,adjudicator_user_id uuid,original_reviewer_count integer,
          recorded_at timestamptz
        ) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT a.task_sha256::text,a.source_sha256::text,a.request_id::text,
            a.candidate_skus,a.finding::text,a.accepted_skus,a.rejected_skus,
            a.no_candidate_match,a.rationale::text,a.adjudicator_user_id,
            a.original_reviewer_count,a.recorded_at
          FROM public.tenant_request_candidate_adjudications a
          WHERE p_org IS NOT NULL AND p_queue ~ '^[a-f0-9]{64}$'
            AND p_limit BETWEEN 1 AND 100 AND p_offset BETWEEN 0 AND 5000
            AND public.kpdesk_session_tenant_allowed(p_org,'read')
            AND EXISTS(
              SELECT 1 FROM public.auth_sessions s
              JOIN public.organization_memberships m
                ON m.organization_id=p_org AND m.user_id=s.user_id
              WHERE s.token_sha256=current_setting('kpdesk.session_sha256',true)
                AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
                AND m.status='active' AND m.role IN ('owner','editor')
            )
            AND a.organization_id=p_org AND a.queue_sha256=p_queue
          ORDER BY a.request_id,a.recorded_at
          LIMIT p_limit OFFSET p_offset
        $$;
    """)
    for signature in (
        "kpdesk_candidate_adjudicate(uuid,text,text,text,text,jsonb,text,jsonb,jsonb,boolean,text)",
        "kpdesk_candidate_adjudications_get(uuid,text,integer,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_tenant_runtime")


def downgrade() -> None:
    raise RuntimeError("Immutable conflict decisions cannot be destroyed by downgrade.")
