"""Verified-account onboarding and durable encrypted email outbox.

Revision ID: 0015_verified_accounts
Revises: 0014_live_auth_rls

No public user is fabricated by migration. New account operations are only
available to kpdesk_auth_runtime through narrow SECURITY DEFINER RPC functions.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0015_verified_accounts"
down_revision = "0014_live_auth_rls"
branch_labels = None
depends_on = None

_CLONE_NAME = re.compile(r"kpdesk_accounts_e2e_[0-9a-f]{12}\Z")

_FUNCS = (
    "kpdesk_account_limit(text,text,text)",
    "kpdesk_account_signup(text,text,text,text,text,text)",
    "kpdesk_account_verify(text)",
    "kpdesk_account_reset_request(text,text,text,text)",
    "kpdesk_account_reset_consume(text,text)",
    "kpdesk_email_claim()",
    "kpdesk_email_complete(uuid,text,boolean)",
)


def upgrade() -> None:
    conn = op.get_bind()
    database = conn.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or not _CLONE_NAME.fullmatch(database):
            raise RuntimeError("0015 requires KPDesk or a marked release clone.")
        marker = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if marker != "kpdesk:accounts-rehearsal:" + database.rsplit("_", 1)[-1]:
            raise RuntimeError("0015 clone provenance check failed.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("0015 requires the migration owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0014_live_auth_rls"
    ):
        raise RuntimeError("0015 requires 0014 live auth/RLS baseline.")
    if conn.scalar(sa.text("""
        SELECT EXISTS(SELECT 1 FROM pg_roles
                      WHERE rolname='kpdesk_auth_runtime' AND NOT rolsuper
                        AND NOT rolcanlogin AND NOT rolbypassrls)
    """)) is not True:
        raise RuntimeError("Separate restricted auth service role is missing.")
    for table in ("account_email_challenges", "account_email_outbox"):
        if conn.scalar(sa.text("SELECT to_regclass(:name)"),
                       {"name": "public." + table}) is not None:
            raise RuntimeError("Account migration tables already exist.")

    op.create_table(
        "account_email_challenges",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(12), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("organization_name", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("purpose IN ('signup','reset')",
                           name="ck_account_challenge_purpose"),
        sa.CheckConstraint("token_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_account_challenge_digest"),
        sa.CheckConstraint(
            "(purpose='signup' AND length(trim(organization_name)) BETWEEN 1 AND 200) "
            "OR (purpose='reset' AND organization_name IS NULL)",
            name="ck_account_challenge_org",
        ),
        sa.CheckConstraint("expires_at > created_at",
                           name="ck_account_challenge_expiry"),
    )
    op.create_index(
        "ix_account_challenges_user", "account_email_challenges",
        ["user_id", "purpose", "created_at"],
    )
    op.create_table(
        "account_email_outbox",
        sa.Column("challenge_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("recipient_email", sa.String(254), nullable=False),
        sa.Column("purpose", sa.String(12), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(256)),
        sa.ForeignKeyConstraint(
            ["challenge_id"], ["account_email_challenges.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("purpose IN ('signup','reset')",
                           name="ck_account_outbox_purpose"),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed')",
            name="ck_account_outbox_status",
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5",
                           name="ck_account_outbox_attempts"),
        sa.CheckConstraint("length(token_ciphertext) BETWEEN 80 AND 4096",
                           name="ck_account_outbox_cipher"),
    )
    op.create_index(
        "ix_account_outbox_pending", "account_email_outbox",
        ["next_attempt_at", "challenge_id"],
        postgresql_where=sa.text("status IN ('pending','sending')"),
    )
    op.create_table(
        "account_action_limits",
        sa.Column("bucket", sa.String(90), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempts BETWEEN 1 AND 2147483647",
                           name="ck_account_action_attempts"),
    )
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_limit(
            p_purpose text, p_email text, p_peer text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            at_time timestamptz := clock_timestamp();
            bucket_account text;
            bucket_peer text;
            account_attempts integer;
            peer_attempts integer;
        BEGIN
            IF p_purpose NOT IN ('signup','reset') OR p_peer IS NULL
               OR length(p_peer) NOT BETWEEN 3 AND 64
               OR p_email IS NULL OR length(p_email)>254 THEN
                RETURN FALSE;
            END IF;
            bucket_account := 'a:' || p_purpose || ':' ||
                encode(sha256(convert_to(p_email,'UTF8')),'hex');
            bucket_peer := 'p:' || p_purpose || ':' ||
                encode(sha256(convert_to(p_peer,'UTF8')),'hex');
            INSERT INTO public.account_action_limits(bucket,window_start,attempts)
            VALUES (LEAST(bucket_account,bucket_peer),at_time,1)
            ON CONFLICT(bucket) DO UPDATE SET
              attempts=CASE WHEN public.account_action_limits.window_start
                < at_time-interval '15 minutes' THEN 1
                ELSE public.account_action_limits.attempts+1 END,
              window_start=CASE WHEN public.account_action_limits.window_start
                < at_time-interval '15 minutes' THEN at_time
                ELSE public.account_action_limits.window_start END;
            INSERT INTO public.account_action_limits(bucket,window_start,attempts)
            VALUES (GREATEST(bucket_account,bucket_peer),at_time,1)
            ON CONFLICT(bucket) DO UPDATE SET
              attempts=CASE WHEN public.account_action_limits.window_start
                < at_time-interval '15 minutes' THEN 1
                ELSE public.account_action_limits.attempts+1 END,
              window_start=CASE WHEN public.account_action_limits.window_start
                < at_time-interval '15 minutes' THEN at_time
                ELSE public.account_action_limits.window_start END;
            SELECT attempts INTO account_attempts
                FROM public.account_action_limits WHERE bucket=bucket_account;
            SELECT attempts INTO peer_attempts
                FROM public.account_action_limits WHERE bucket=bucket_peer;
            RETURN account_attempts<=4 AND peer_attempts<=20;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_signup(
            p_email text, p_password_hash text, p_org_name text,
            p_digest text, p_cipher text, p_peer text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            new_user uuid := gen_random_uuid();
            new_challenge uuid := gen_random_uuid();
        BEGIN
            IF p_email IS NULL OR length(p_email)>254
               OR p_email<>lower(trim(p_email))
               OR p_email !~ '^[a-z0-9][a-z0-9._%+\\-]{0,63}@[a-z0-9.-]+\\.[a-z]{2,30}$'
               OR p_password_hash IS NULL
               OR p_password_hash NOT LIKE '$argon2id$%'
               OR length(p_password_hash)>512
               OR p_org_name IS NULL
               OR length(trim(p_org_name)) NOT BETWEEN 1 AND 200
               OR p_digest !~ '^[0-9a-f]{64}$'
               OR length(p_cipher) NOT BETWEEN 80 AND 4096 THEN
                RETURN FALSE;
            END IF;
            IF NOT public.kpdesk_account_limit('signup',p_email,p_peer) THEN
                RETURN FALSE;
            END IF;
            -- Avoid duplicate signup races and do not reveal existing identities.
            INSERT INTO public.users(
                id,email,password_hash,status
            ) VALUES (
                new_user,p_email,p_password_hash,'pending'
            ) ON CONFLICT DO NOTHING;
            IF NOT FOUND THEN
                RETURN FALSE;
            END IF;
            INSERT INTO public.account_email_challenges(
                id,user_id,purpose,token_sha256,organization_name,expires_at
            ) VALUES (
                new_challenge,new_user,'signup',p_digest,trim(p_org_name),
                clock_timestamp()+interval '30 minutes'
            );
            INSERT INTO public.account_email_outbox(
                challenge_id,recipient_email,purpose,token_ciphertext
            ) VALUES (new_challenge,p_email,'signup',p_cipher);
            RETURN TRUE;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_verify(p_digest text)
        RETURNS TABLE(identity_id uuid,organization_id uuid)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            candidate record;
            created_org uuid := gen_random_uuid();
        BEGIN
            IF p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$' THEN
                RETURN;
            END IF;
            SELECT ch.id,ch.user_id,ch.organization_name
            INTO candidate
            FROM public.account_email_challenges ch
            JOIN public.users u ON u.id=ch.user_id
            WHERE ch.token_sha256=p_digest AND ch.purpose='signup'
              AND ch.consumed_at IS NULL AND ch.expires_at>clock_timestamp()
              AND u.status='pending' AND u.email_verified_at IS NULL
            FOR UPDATE OF ch;
            IF NOT FOUND THEN RETURN; END IF;
            UPDATE public.users SET
                status='active',email_verified_at=clock_timestamp()
            WHERE id=candidate.user_id AND status='pending';
            IF NOT FOUND THEN RETURN; END IF;
            INSERT INTO public.organizations(id,name,status)
                VALUES (created_org,candidate.organization_name,'active');
            INSERT INTO public.organization_memberships(
                organization_id,user_id,role,status
            ) VALUES (created_org,candidate.user_id,'owner','active');
            UPDATE public.account_email_challenges
                SET consumed_at=clock_timestamp() WHERE id=candidate.id;
            identity_id := candidate.user_id;
            organization_id := created_org;
            RETURN NEXT;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_reset_request(
            p_email text,p_digest text,p_cipher text,p_peer text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            existing_user uuid;
            next_id uuid := gen_random_uuid();
        BEGIN
            IF p_email IS NULL OR length(p_email)>254
               OR p_email<>lower(trim(p_email))
               OR p_digest !~ '^[0-9a-f]{64}$'
               OR length(p_cipher) NOT BETWEEN 80 AND 4096 THEN
                RETURN FALSE;
            END IF;
            IF NOT public.kpdesk_account_limit('reset',p_email,p_peer) THEN
                RETURN FALSE;
            END IF;
            SELECT id INTO existing_user FROM public.users
                WHERE email=p_email AND status='active'
                  AND email_verified_at IS NOT NULL
                FOR UPDATE;
            IF NOT FOUND THEN RETURN FALSE; END IF;
            UPDATE public.account_email_challenges SET consumed_at=clock_timestamp()
                WHERE user_id=existing_user AND purpose='reset'
                  AND consumed_at IS NULL;
            INSERT INTO public.account_email_challenges(
                id,user_id,purpose,token_sha256,expires_at
            ) VALUES(
                next_id,existing_user,'reset',p_digest,
                clock_timestamp()+interval '30 minutes'
            );
            INSERT INTO public.account_email_outbox(
                challenge_id,recipient_email,purpose,token_ciphertext
            ) VALUES(next_id,p_email,'reset',p_cipher);
            RETURN TRUE;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_account_reset_consume(
            p_digest text,p_new_hash text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            selected record;
        BEGIN
            IF p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
               OR p_new_hash IS NULL
               OR p_new_hash NOT LIKE '$argon2id$%'
               OR length(p_new_hash)>512 THEN RETURN FALSE; END IF;
            SELECT c.id,c.user_id INTO selected
                FROM public.account_email_challenges c
                JOIN public.users u ON u.id=c.user_id
                WHERE c.token_sha256=p_digest AND c.purpose='reset'
                  AND c.consumed_at IS NULL AND c.expires_at>clock_timestamp()
                  AND u.status='active' AND u.email_verified_at IS NOT NULL
                FOR UPDATE OF c;
            IF NOT FOUND THEN RETURN FALSE; END IF;
            UPDATE public.users SET password_hash=p_new_hash
                WHERE id=selected.user_id;
            UPDATE public.auth_sessions SET revoked_at=clock_timestamp()
                WHERE user_id=selected.user_id AND revoked_at IS NULL;
            UPDATE public.account_email_challenges SET consumed_at=clock_timestamp()
                WHERE id=selected.id;
            RETURN TRUE;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_email_claim()
        RETURNS TABLE(challenge_id uuid, recipient_email text,
                      purpose text, token_ciphertext text)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            selected record;
        BEGIN
            SELECT q.challenge_id,q.recipient_email,q.purpose,q.token_ciphertext
            INTO selected
            FROM public.account_email_outbox q
            JOIN public.account_email_challenges c ON c.id=q.challenge_id
            WHERE q.status IN ('pending','sending')
              AND q.attempts<5 AND q.next_attempt_at<=clock_timestamp()
              AND (q.lease_expires_at IS NULL
                   OR q.lease_expires_at<clock_timestamp())
              AND c.consumed_at IS NULL AND c.expires_at>clock_timestamp()
            ORDER BY q.next_attempt_at,q.challenge_id
            FOR UPDATE OF q SKIP LOCKED LIMIT 1;
            IF NOT FOUND THEN RETURN; END IF;
            UPDATE public.account_email_outbox q
                SET status='sending',attempts=q.attempts+1,
                    lease_expires_at=clock_timestamp()+interval '45 seconds'
            WHERE q.challenge_id=selected.challenge_id;
            challenge_id := selected.challenge_id;
            recipient_email := selected.recipient_email;
            purpose := selected.purpose;
            token_ciphertext := selected.token_ciphertext;
            RETURN NEXT;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_email_complete(
            p_id uuid,p_provider_id text,p_success boolean
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF p_success IS NULL OR (p_success AND
               (p_provider_id IS NULL OR length(p_provider_id) NOT BETWEEN 1 AND 256))
            THEN RETURN FALSE; END IF;
            UPDATE public.account_email_outbox q SET
                status=CASE WHEN p_success THEN 'sent'
                     WHEN q.attempts>=5 THEN 'failed' ELSE 'pending' END,
                sent_at=CASE WHEN p_success THEN clock_timestamp() ELSE NULL END,
                provider_message_id=CASE WHEN p_success THEN p_provider_id ELSE NULL END,
                lease_expires_at=NULL,
                next_attempt_at=clock_timestamp()+
                    make_interval(secs => LEAST(900,30 * (1 << LEAST(4,q.attempts))))
            WHERE q.challenge_id=p_id AND q.status='sending'
              AND q.lease_expires_at>=clock_timestamp()
              AND q.attempts BETWEEN 1 AND 5;
            RETURN FOUND;
        END $$;
    """)
    for signature in _FUNCS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    for signature in _FUNCS[1:]:
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} TO kpdesk_auth_runtime"
        )
    # Nobody, including the other tenant runtime and old kpdesk_app, receives
    # direct access to users, outbox plaintext envelope or pending challenges.


def downgrade() -> None:
    raise RuntimeError(
        "Verified-account release cannot be automatically downgraded; "
        "account tokens, delivery queue and personal data require reviewed recovery."
    )
