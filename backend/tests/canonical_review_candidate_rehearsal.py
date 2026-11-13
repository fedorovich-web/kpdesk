"""A93 real Alembic rehearsal, restricted to a provenance-tagged temporary PG clone."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from canonical_review_authenticated_rehearsal import verify_authenticated_rotation_on_clone
from canonical_review_rotation_cohort_rehearsal import verify_review_rotation_cohort
from sqlalchemy import Engine, text

from kpdesk.catalog.review_signing import ReviewSigningCredential
from kpdesk.db.live_rehearsal_scope import is_live_accounts_http_clone
from kpdesk.db.review_rollout_audit import audit_review_schema
from kpdesk.db.review_schema_activation import (
    activate_review_schema_on_clone,
    deactivate_review_schema_on_clone,
)
from kpdesk.db.review_schema_manifest import REVIEW_TABLES
from kpdesk.db.review_signer_provision import (
    ReviewSignerProvisionRejected,
    provision_review_signer_on_clone,
    verify_review_signer,
)


def verify_candidate_upgrade_downgrade(engine: Engine) -> None:
    """Install inactive schema, reject data loss, uninstall without dropping pgcrypto."""
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("version_locations", os.pathsep.join((
        str(backend / "migrations" / "versions"),
        str(backend / "migrations" / "candidate_versions"),
    )))
    config.attributes["a93_schema_candidate_approved"] = True
    with engine.begin() as connection:
        if (
            not is_live_accounts_http_clone(connection)
            or connection.scalar(text("SELECT current_user")) != "kpdesk"
            or connection.scalar(text(
                "SELECT version_num FROM public.alembic_version"
            )) != "0030_tenant_llm_cache"
        ):
            raise RuntimeError("A93 rehearsal requires verified tagged owner clone.")
        assert connection.scalar(text(
            "SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'"
        )) == 0

        # Even the prerequisite extension is rolled back by the outer savepoint.
        # The deployed database is not this temporary clone.
        with connection.begin_nested() as cleanup:
            connection.execute(text(
                "CREATE EXTENSION pgcrypto WITH SCHEMA public"
            ))
            config.attributes["connection"] = connection
            command.upgrade(config, "0031_review_schema_candidate")
            assert connection.scalar(text(
                "SELECT version_num FROM public.alembic_version"
            )) == "0031_review_schema_candidate"

            audit = audit_review_schema(
                connection, expected_revision="0031_review_schema_candidate",
            )
            assert not audit.ready
            assert audit.signing_epoch is None
            assert "invalid_signer_metadata" in audit.issues
            assert any(
                issue.startswith("function_grant_mismatch:kpdesk_tenant_runtime:")
                for issue in audit.issues
            )
            assert all(
                issue == "invalid_signer_metadata"
                or issue.startswith("function_grant_mismatch:")
                for issue in audit.issues
            ), audit.issues
            assert connection.scalar(text(
                "SELECT count(*) FROM public.a84_provenance_signing_secret"
            )) == 0
            assert connection.scalar(text("""
                SELECT has_function_privilege(
                  'kpdesk_tenant_runtime',
                  'public.a79_review_snapshot(uuid,text,text,text,text,text,uuid)',
                  'EXECUTE'
                )
            """)) is False

            # Exercise actual DB cryptographic proof and stale worker refusal.
            with connection.begin_nested() as signing_rehearsal:
                first = ReviewSigningCredential(epoch=1, key=secrets.token_bytes(32))
                second = ReviewSigningCredential(epoch=2, key=secrets.token_bytes(32))
                incorrect = ReviewSigningCredential(epoch=1, key=secrets.token_bytes(32))
                assert not verify_review_signer(connection, first)
                try:
                    activate_review_schema_on_clone(connection, first)
                except ReviewSignerProvisionRejected:
                    pass
                else:
                    raise AssertionError("A93 activated schema without DB signer.")
                # Inject post-insert verification failure: the inner SAVEPOINT
                # must undo the insert even if the caller handles the error.
                with patch(
                    "kpdesk.db.review_signer_provision.verify_review_signer",
                    return_value=False,
                ):
                    try:
                        provision_review_signer_on_clone(
                            connection, first, expected_latest_epoch=None,
                        )
                    except ReviewSignerProvisionRejected:
                        pass
                    else:
                        raise AssertionError("A93 accepted invalid DB signer proof.")
                assert connection.scalar(text(
                    "SELECT count(*) FROM public.a84_provenance_signing_secret"
                )) == 0
                provision_review_signer_on_clone(
                    connection, first, expected_latest_epoch=None,
                )
                assert verify_review_signer(connection, first)
                assert not verify_review_signer(connection, incorrect)
                try:
                    activate_review_schema_on_clone(connection, incorrect)
                except ReviewSignerProvisionRejected:
                    pass
                else:
                    raise AssertionError("A93 activated mismatched signer.")
                activate_review_schema_on_clone(connection, first)
                audit_review_schema(
                    connection, expected_revision="0031_review_schema_candidate",
                ).require_ready()
                # Refuse epoch switch even with a correct expected epoch while
                # runtime functions are active; old workers could still sign.
                try:
                    provision_review_signer_on_clone(
                        connection, second, expected_latest_epoch=1,
                    )
                except ReviewSignerProvisionRejected as error:
                    assert "not quiesced" in str(error)
                else:
                    raise AssertionError("A94 rotated key with active SQL ACLs.")
                assert verify_review_signer(connection, first)
                deactivate_review_schema_on_clone(connection)
                deactivate_review_schema_on_clone(connection)
                assert not audit_review_schema(
                    connection, expected_revision="0031_review_schema_candidate",
                ).ready
                # A drifted table ACL must block rotation even while the
                # nominally expected EXECUTE privileges are revoked.
                with connection.begin_nested() as drift:
                    connection.execute(text("""
                        GRANT SELECT ON public.a84_provenance_signing_secret
                        TO kpdesk_tenant_runtime
                    """))
                    try:
                        provision_review_signer_on_clone(
                            connection, second, expected_latest_epoch=1,
                        )
                    except ReviewSignerProvisionRejected as error:
                        assert "not quiesced" in str(error)
                    else:
                        raise AssertionError("A94 ignored unsafe signer table ACL.")
                    drift.rollback()
                try:
                    provision_review_signer_on_clone(
                        connection, second, expected_latest_epoch=None,
                    )
                except ReviewSignerProvisionRejected:
                    pass
                else:
                    raise AssertionError("A93 accepted stale rotation precondition.")
                provision_review_signer_on_clone(
                    connection, second, expected_latest_epoch=1,
                )
                assert verify_review_signer(connection, second)
                assert not verify_review_signer(connection, first)
                assert not verify_review_signer(connection, incorrect)
                try:
                    activate_review_schema_on_clone(connection, first)
                except ReviewSignerProvisionRejected:
                    pass
                else:
                    raise AssertionError("A93 accepted stale worker for activation.")
                activate_review_schema_on_clone(connection, second)
                audit_review_schema(
                    connection, expected_revision="0031_review_schema_candidate",
                ).require_ready()
                deactivate_review_schema_on_clone(connection)
                assert connection.scalar(text(
                    "SELECT count(*) FROM public.a84_provenance_signing_secret"
                )) == 2
                signing_rehearsal.rollback()
            assert connection.scalar(text(
                "SELECT count(*) FROM public.a84_provenance_signing_secret"
            )) == 0

            verify_review_rotation_cohort(connection)
            verify_authenticated_rotation_on_clone(connection)

            with connection.begin_nested() as nonempty:
                connection.execute(text("""
                    INSERT INTO public.a84_provenance_signing_secret
                    (key_epoch,secret) VALUES (1,decode(repeat('ab',32),'hex'))
                """))
                try:
                    command.downgrade(config, "0030_tenant_llm_cache")
                except RuntimeError as error:
                    assert "nonempty" in str(error)
                else:
                    raise AssertionError("A93 erased a provisioned signing key.")
                nonempty.rollback()

            command.downgrade(config, "0030_tenant_llm_cache")
            assert connection.scalar(text(
                "SELECT version_num FROM public.alembic_version"
            )) == "0030_tenant_llm_cache"
            assert all(
                connection.scalar(
                    text("SELECT to_regclass(:relation)"),
                    {"relation": "public." + table},
                ) is None
                for table in REVIEW_TABLES
            )
            assert connection.scalar(text(
                "SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'"
            )) == 1
            cleanup.rollback()
        assert connection.scalar(text(
            "SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'"
        )) == 0
        assert connection.scalar(text(
            "SELECT version_num FROM public.alembic_version"
        )) == "0030_tenant_llm_cache"
