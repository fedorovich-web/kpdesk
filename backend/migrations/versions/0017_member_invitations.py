"""A17 — verified-user invitations and restricted membership administration.

Revision ID: 0017_member_invitations
Revises: 0016_account_resend_orgs

Invitation acceptance is explicit. No direct privileges on user/membership
tables. Owner rights are never transferred or deleted by these functions.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0017_member_invitations"
down_revision = "0016_account_resend_orgs"
branch_labels = None
depends_on = None

_CLONE = re.compile(r"kpdesk_accounts_http_[0-9a-f]{12}\Z")


def upgrade() -> None:
    conn = op.get_bind()
    db = conn.scalar(sa.text("SELECT current_database()"))
    if db != "kpdesk":
        if not isinstance(db, str) or _CLONE.fullmatch(db) is None:
            raise RuntimeError("A17 requires KPDesk or a marked A17 test copy.")
        mark = conn.scalar(sa.text("""
            SELECT shobj_description(oid,'pg_database')
            FROM pg_database WHERE datname=:db
        """), {"db": db})
        if mark != "kpdesk:accounts-http:" + db.rsplit("_", 1)[-1]:
            raise RuntimeError("A17 test database provenance failed.")
    if conn.scalar(sa.text("SELECT current_user")) != "kpdesk":
        raise RuntimeError("A17 requires local PostgreSQL owner.")
    if conn.scalar(sa.text("SELECT version_num FROM alembic_version")) != (
        "0016_account_resend_orgs"
    ):
        raise RuntimeError("A17 requires live release 0016.")
    if conn.scalar(sa.text(
        "SELECT to_regclass('public.organization_invitations')"
    )) is not None:
        raise RuntimeError("Invitation table unexpectedly exists.")

    op.create_table(
        "organization_invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("invited_by", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()+interval '7 days'")),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("role IN ('editor','viewer')",
                           name="ck_invitation_non_owner"),
        sa.CheckConstraint("status IN ('pending','accepted','revoked')",
                           name="ck_invitation_status"),
        sa.CheckConstraint("expires_at>created_at",
                           name="ck_invitation_valid_expiry"),
    )
    op.create_index("ix_org_invitation_user_status", "organization_invitations",
                    ["user_id", "status", "expires_at"])
    op.create_index("ix_org_invitation_org_status", "organization_invitations",
                    ["organization_id", "status"])

    # Validate a real active session, not browser-supplied user/org headers.
    op.execute("""
        CREATE FUNCTION public.kpdesk_member_actor(
            p_digest text, p_organization uuid, p_owner boolean
        ) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT u.id FROM public.auth_sessions s
          JOIN public.users u ON u.id=s.user_id
          JOIN public.organization_memberships m ON m.user_id=u.id
          JOIN public.organizations o ON o.id=m.organization_id
          WHERE p_digest ~ '^[0-9a-f]{64}$'
            AND s.token_sha256=p_digest
            AND s.revoked_at IS NULL AND s.expires_at>statement_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND m.organization_id=p_organization
            AND m.status='active' AND o.status='active'
            AND (NOT p_owner OR m.role='owner')
          LIMIT 1
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_member_invite(
            p_digest text, p_organization uuid, p_email text, p_role text
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            actor uuid;
            invited uuid;
            invited_id uuid;
        BEGIN
            IF p_role NOT IN ('viewer','editor')
                OR p_email IS NULL OR length(p_email)>254
                OR p_email<>lower(trim(p_email))
            THEN RETURN NULL; END IF;
            actor := public.kpdesk_member_actor(p_digest,p_organization,true);
            IF actor IS NULL THEN RETURN NULL; END IF;
            SELECT id INTO invited FROM public.users
                WHERE email=p_email AND status='active'
                    AND email_verified_at IS NOT NULL;
            IF invited IS NULL OR invited=actor THEN RETURN NULL; END IF;
            -- Atomic reservation via a transaction-scoped lock per org/user.
            PERFORM pg_advisory_xact_lock(
              hashtextextended(p_organization::text||':'||invited::text,0)
            );
            IF EXISTS(
                SELECT 1 FROM public.organization_memberships
                WHERE organization_id=p_organization AND user_id=invited
                  AND status='active'
            ) THEN RETURN NULL; END IF;
            IF EXISTS(
                SELECT 1 FROM public.organization_invitations
                WHERE organization_id=p_organization AND user_id=invited
                  AND status='pending' AND expires_at>clock_timestamp()
            ) THEN RETURN NULL; END IF;
            UPDATE public.organization_invitations SET status='revoked'
              WHERE organization_id=p_organization AND user_id=invited
                AND status='pending';
            invited_id := gen_random_uuid();
            INSERT INTO public.organization_invitations(
                id,organization_id,user_id,invited_by,role
            ) VALUES(invited_id,p_organization,invited,actor,p_role);
            RETURN invited_id;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_member_pending(p_digest text)
        RETURNS TABLE(invitation_id uuid, organization_id uuid,
                      organization_name text, proposed_role text,
                      expires_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT i.id,o.id,o.name::text,i.role::text,i.expires_at
          FROM public.organization_invitations i
          JOIN public.organizations o ON o.id=i.organization_id
          JOIN public.auth_sessions s ON s.user_id=i.user_id
          JOIN public.users u ON u.id=s.user_id
          WHERE p_digest ~ '^[0-9a-f]{64}$'
            AND s.token_sha256=p_digest AND s.revoked_at IS NULL
            AND s.expires_at>statement_timestamp()
            AND u.status='active' AND u.email_verified_at IS NOT NULL
            AND i.status='pending' AND i.expires_at>statement_timestamp()
            AND o.status='active'
          ORDER BY i.created_at,i.id
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_member_accept(
            p_digest text,p_invitation uuid
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            session_user_id uuid;
            invitation record;
        BEGIN
            SELECT u.id INTO session_user_id
            FROM public.auth_sessions s JOIN public.users u ON u.id=s.user_id
            WHERE p_digest ~ '^[0-9a-f]{64}$' AND s.token_sha256=p_digest
              AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
              AND u.status='active' AND u.email_verified_at IS NOT NULL;
            IF session_user_id IS NULL THEN RETURN NULL; END IF;
            SELECT i.id,i.organization_id,i.user_id,i.role INTO invitation
            FROM public.organization_invitations i
            JOIN public.organizations o ON o.id=i.organization_id
            WHERE i.id=p_invitation AND i.user_id=session_user_id
              AND i.status='pending' AND i.expires_at>clock_timestamp()
              AND o.status='active' FOR UPDATE OF i;
            IF NOT FOUND THEN RETURN NULL; END IF;
            -- Never overwrite or reactivate an existing membership, even
            -- revoked ones: owner must explicitly review those cases.
            IF EXISTS(SELECT 1 FROM public.organization_memberships
               WHERE organization_id=invitation.organization_id
                 AND user_id=session_user_id) THEN RETURN NULL; END IF;
            INSERT INTO public.organization_memberships(
              organization_id,user_id,role,status
            ) VALUES(invitation.organization_id,session_user_id,
                     invitation.role,'active');
            UPDATE public.organization_invitations
                SET status='accepted',accepted_at=clock_timestamp()
                WHERE id=p_invitation;
            RETURN invitation.organization_id;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_member_list(
            p_digest text,p_organization uuid
        ) RETURNS TABLE(user_id uuid,member_email text,
                        membership_role text,membership_status text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
          SELECT m.user_id,u.email::text,m.role::text,m.status::text
          FROM public.organization_memberships m
          JOIN public.users u ON u.id=m.user_id
          WHERE m.organization_id=p_organization
            AND public.kpdesk_member_actor(
               p_digest,p_organization,true
            ) IS NOT NULL
          ORDER BY u.email
        $$;
    """)
    op.execute("""
        CREATE FUNCTION public.kpdesk_member_change(
            p_digest text,p_organization uuid,p_target uuid,
            p_role text,p_revoke boolean
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            actor uuid;
        BEGIN
            IF p_role NOT IN ('viewer','editor') OR p_revoke IS NULL
            THEN RETURN FALSE; END IF;
            actor := public.kpdesk_member_actor(
                p_digest,p_organization,true
            );
            IF actor IS NULL OR actor=p_target THEN RETURN FALSE; END IF;
            UPDATE public.organization_memberships m SET
                role=CASE WHEN p_revoke THEN m.role ELSE p_role END,
                status=CASE WHEN p_revoke THEN 'revoked' ELSE 'active' END
            WHERE m.organization_id=p_organization AND m.user_id=p_target
              AND m.status='active' AND m.role IN ('editor','viewer');
            RETURN FOUND;
        END $$;
    """)
    for signature in (
        "kpdesk_member_actor(text,uuid,boolean)",
        "kpdesk_member_invite(text,uuid,text,text)",
        "kpdesk_member_pending(text)",
        "kpdesk_member_accept(text,uuid)",
        "kpdesk_member_list(text,uuid)",
        "kpdesk_member_change(text,uuid,uuid,text,boolean)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.{signature} "
            "TO kpdesk_tenant_runtime"
        )


def downgrade() -> None:
    raise RuntimeError("Invitation/membership downgrade requires reviewed account recovery.")
