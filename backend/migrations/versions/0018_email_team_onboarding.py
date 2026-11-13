"""A18 verified email invitations for previously unregistered users.

Revision ID: 0018_email_team_onboarding
Revises: 0017_member_invitations
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0018_email_team_onboarding"
down_revision = "0017_member_invitations"
branch_labels = None
depends_on = None
_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    database = conn.scalar(sa.text("SELECT current_database()"))
    if database != "kpdesk":
        if not isinstance(database, str) or _CLONE.fullmatch(database) is None:
            raise RuntimeError("A18 requires working database or marked auth clone.")
        expected = "kpdesk:accounts-http:" + database.rsplit("_", 1)[-1]
        mark = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:name
        """), {"name": database})
        if mark != expected:
            raise RuntimeError("A18 test clone provenance check failed.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A18 requires reviewed owner credential.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0017_member_invitations"
    ):
        raise RuntimeError("A18 must directly follow A17.")
    if conn.scalar(sa.text(
        "SELECT to_regclass('public.organization_email_invitations')"
    )) is not None:
        raise RuntimeError("A18 invitations table unexpectedly exists.")

    op.create_table(
        "organization_email_invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("invited_by", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()+interval '7 days'")),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(256)),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"],
                                ondelete="RESTRICT"),
        sa.CheckConstraint("role IN ('viewer','editor')",
                           name="ck_email_invite_role"),
        sa.CheckConstraint("status IN ('pending','sending','sent','failed','used')",
                           name="ck_email_invite_status"),
        sa.CheckConstraint("token_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_email_invite_digest"),
        sa.CheckConstraint("length(token_ciphertext) BETWEEN 80 AND 4096",
                           name="ck_email_invite_ciphertext"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5",
                           name="ck_email_invite_attempts"),
        sa.CheckConstraint("expires_at>created_at",
                           name="ck_email_invite_expiry"),
    )
    op.create_index("ix_email_invite_mail", "organization_email_invitations",
                    ["next_attempt_at", "id"],
                    postgresql_where=sa.text(
                        "status IN ('pending','sending') AND attempts<5"
                    ))
    op.create_index("ix_email_invite_org_recipient", "organization_email_invitations",
                    ["organization_id", "email", "created_at"])

    op.execute("""
        CREATE FUNCTION public.kpdesk_email_invite_issue(
            p_session text,p_org uuid,p_email text,p_role text,
            p_digest text,p_ciphertext text
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE actor uuid; next_id uuid := gen_random_uuid();
        BEGIN
            IF p_role NOT IN ('editor','viewer')
              OR p_email IS NULL OR length(p_email)>254
              OR p_email<>lower(trim(p_email))
              OR p_email !~ '^[a-z0-9][a-z0-9._%+\\-]{0,63}@[a-z0-9.-]+\\.[a-z]{2,30}$'
              OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
              OR p_ciphertext IS NULL
              OR length(p_ciphertext) NOT BETWEEN 80 AND 4096
            THEN RETURN NULL; END IF;
            actor:=public.kpdesk_member_actor(p_session,p_org,true);
            IF actor IS NULL THEN RETURN NULL; END IF;
            -- Lock the selected organization so two owners cannot race a
            -- duplicate invitation or exceed a daily invitation quota.
            PERFORM 1 FROM public.organizations o WHERE o.id=p_org FOR UPDATE;
            IF EXISTS(SELECT 1 FROM public.users WHERE email=p_email)
            THEN RETURN NULL; END IF;
            IF (SELECT count(*) FROM public.organization_email_invitations
                WHERE organization_id=p_org
                  AND created_at>clock_timestamp()-interval '1 day')>=20
            THEN RETURN NULL; END IF;
            IF EXISTS(
                SELECT 1 FROM public.organization_email_invitations
                WHERE organization_id=p_org AND email=p_email
                  AND expires_at>clock_timestamp()
                  AND status NOT IN ('failed','used')
            ) THEN RETURN NULL; END IF;
            INSERT INTO public.organization_email_invitations(
                id,organization_id,email,invited_by,role,
                token_sha256,token_ciphertext
            ) VALUES(next_id,p_org,p_email,actor,p_role,p_digest,p_ciphertext);
            RETURN next_id;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_email_invite_claim()
        RETURNS TABLE(invite_id uuid, recipient text, ciphertext text)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE selected record;
        BEGIN
            SELECT i.id,i.email,i.token_ciphertext INTO selected
            FROM public.organization_email_invitations i
            WHERE i.status IN ('pending','sending') AND i.attempts<5
              AND i.next_attempt_at<=clock_timestamp()
              AND i.expires_at>clock_timestamp()
              AND i.consumed_at IS NULL
              AND (i.lease_expires_at IS NULL
                   OR i.lease_expires_at<clock_timestamp())
            ORDER BY i.next_attempt_at,i.id
            LIMIT 1 FOR UPDATE OF i SKIP LOCKED;
            IF NOT FOUND THEN RETURN; END IF;
            UPDATE public.organization_email_invitations
            SET status='sending',attempts=attempts+1,
                lease_expires_at=clock_timestamp()+interval '45 seconds'
            WHERE id=selected.id;
            invite_id:=selected.id;
            recipient:=selected.email;
            ciphertext:=selected.token_ciphertext;
            RETURN NEXT;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_email_invite_complete(
            p_id uuid,p_provider text,p_sent boolean
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF p_sent IS NULL OR (p_sent AND
                 (p_provider IS NULL OR length(p_provider) NOT BETWEEN 1 AND 256))
            THEN RETURN FALSE; END IF;
            UPDATE public.organization_email_invitations SET
                status=CASE WHEN p_sent THEN 'sent'
                    WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                sent_at=CASE WHEN p_sent THEN clock_timestamp() ELSE NULL END,
                provider_message_id=CASE WHEN p_sent THEN p_provider ELSE NULL END,
                lease_expires_at=NULL,
                next_attempt_at=clock_timestamp()+
                    make_interval(secs => LEAST(900,30*(1<<LEAST(attempts,4))))
            WHERE id=p_id AND status='sending'
              AND lease_expires_at>=clock_timestamp()
              AND attempts BETWEEN 1 AND 5;
            RETURN FOUND;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_email_invite_accept(
            p_digest text,p_hash text
        ) RETURNS TABLE(user_id uuid,organization_id uuid)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            candidate record;
            new_user uuid:=gen_random_uuid();
        BEGIN
            IF p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
              OR p_hash IS NULL OR p_hash NOT LIKE '$argon2id$%'
              OR length(p_hash)>512 THEN RETURN; END IF;
            SELECT i.id,i.organization_id,i.email,i.role INTO candidate
            FROM public.organization_email_invitations i
            JOIN public.organizations o ON o.id=i.organization_id
            WHERE i.token_sha256=p_digest AND i.status IN
                ('pending','sending','sent') AND i.consumed_at IS NULL
              AND i.expires_at>clock_timestamp() AND o.status='active'
            FOR UPDATE OF i;
            IF NOT FOUND THEN RETURN; END IF;
            -- Email ownership is proven by possession of the one-use link.
            -- Conflicting signup or duplicate claims may never add membership.
            IF EXISTS(SELECT 1 FROM public.users WHERE email=candidate.email)
            THEN RETURN; END IF;
            INSERT INTO public.users(
                id,email,password_hash,email_verified_at,status
            ) VALUES(
                new_user,candidate.email,p_hash,clock_timestamp(),'active'
            ) ON CONFLICT DO NOTHING;
            IF NOT FOUND THEN RETURN; END IF;
            INSERT INTO public.organization_memberships(
                organization_id,user_id,role,status
            ) VALUES(candidate.organization_id,new_user,candidate.role,'active');
            UPDATE public.organization_email_invitations SET
                consumed_at=clock_timestamp(),status='used'
            WHERE id=candidate.id;
            user_id:=new_user;
            organization_id:=candidate.organization_id;
            RETURN NEXT;
        END $$;
    """)
    for signature, role in (
        ("kpdesk_email_invite_issue(text,uuid,text,text,text,text)",
         "kpdesk_tenant_runtime"),
        ("kpdesk_email_invite_claim()", "kpdesk_auth_runtime"),
        ("kpdesk_email_invite_complete(uuid,text,boolean)", "kpdesk_auth_runtime"),
        ("kpdesk_email_invite_accept(text,text)", "kpdesk_auth_runtime"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {role}")


def downgrade() -> None:
    raise RuntimeError(
        "A18 email invitation state cannot be automatically downgraded."
    )
