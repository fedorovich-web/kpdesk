"""Tenant model insertion and database denial tests on fresh marked clones."""

import os
import uuid
from collections.abc import Iterator

import pytest
from postgres_clone_connection import LEGACY_TEST_OPT_IN, legacy_test_database
from psycopg.errors import CheckViolation
from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.skipif(
    os.environ.get(LEGACY_TEST_OPT_IN) != "1",
    reason="Explicit opt-in required for per-test marked disposable PostgreSQL clones",
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    with legacy_test_database() as engine:
        yield engine


def test_tenant_models_and_role_constraints_rollback_only(engine: Engine) -> None:
    from kpdesk.db.tenant_models import Organization, OrganizationMembership, User

    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(insert(Organization).values(
                id=org_id, name="Synthetic A", status="active",
            ))
            connection.execute(insert(User).values(
                id=user_id, email=f"test-{user_id}@example.invalid", status="pending",
            ))
            connection.execute(insert(OrganizationMembership).values(
                organization_id=org_id, user_id=user_id, role="viewer", status="active",
            ))
            extra_id = uuid.uuid4()
            connection.execute(insert(User).values(
                id=extra_id, email=f"test-{extra_id}@example.invalid", status="pending",
            ))
            with pytest.raises(IntegrityError) as invalid_role, connection.begin_nested():
                connection.execute(insert(OrganizationMembership).values(
                    organization_id=org_id, user_id=extra_id,
                    role="administrator", status="active",
                ))
            assert isinstance(invalid_role.value.orig, CheckViolation)
            assert invalid_role.value.orig.diag.constraint_name == "ck_membership_role"
            with pytest.raises(IntegrityError) as invalid_email, connection.begin_nested():
                connection.execute(insert(User).values(
                    id=uuid.uuid4(), email="UPPER@example.invalid", status="pending",
                ))
            assert isinstance(invalid_email.value.orig, CheckViolation)
            assert invalid_email.value.orig.diag.constraint_name == "ck_user_normalized_email"
        finally:
            transaction.rollback()
