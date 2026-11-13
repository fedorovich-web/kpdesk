"""A4 rollback-only PostgreSQL store for reviewed supplier layout profiles.

No HTTP endpoint, no DB migration on production, no ML label publication.
Persists only profiles with verified SHA256 and trusted membership; the caller
owns transaction and must roll it back if a later operation fails.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.catalog.supplier_profile_store import (
    AmbiguousSupplierProfiles,
    _validate_loaded_document,
)
from kpdesk.catalog.supplier_profiles import ReviewedSupplierProfile
from kpdesk.db.tenant_access import (
    AuthenticatedUser,
    TenantAccessDenied,
    authorize_test_tenant_runtime,
)

_INSERT = text("""
    INSERT INTO supplier_reviewed_profiles (
        organization_id, supplier_id, schema_fingerprint, profile_sha256,
        source_sha256, approved_by, profile_json
    ) VALUES (
        :org, :supplier, :fingerprint, :profile_hash,
        :source_hash, :reviewer, :document
    ) ON CONFLICT (
        organization_id, supplier_id, schema_fingerprint, profile_sha256
    ) DO NOTHING
""").bindparams(bindparam("document", type_=JSONB))


def _document(profile: ReviewedSupplierProfile) -> dict[str, object]:
    data = asdict(profile)
    data["organization_id"] = str(profile.organization_id)
    data["approved_by"] = str(profile.approved_by)
    # Normalize nested frozen tuples and ensure PostgreSQL JSONB receives
    # only pure JSON values. No implicit stringification of unknown types.
    result: dict[str, object] = json.loads(
        json.dumps(data, ensure_ascii=False, allow_nan=False)
    )
    _validate_loaded_document(result)
    return result


@dataclass(frozen=True, slots=True)
class TenantSupplierProfileStore:
    connection: Connection
    identity: AuthenticatedUser
    organization_id: uuid.UUID

    def save(self, profile: ReviewedSupplierProfile) -> ReviewedSupplierProfile:
        """Idempotent immutable reviewed profile insert, tenant+reviewer verified."""
        authorize_test_tenant_runtime(
            self.connection, self.identity, self.organization_id, "manage",
        )
        if (
            profile.organization_id != self.organization_id
            or profile.approved_by != self.identity.user_id
        ):
            raise TenantAccessDenied("Reviewed profile belongs to another reviewer/tenant.")
        document = _document(profile)
        self.connection.execute(_INSERT, {
            "org": self.organization_id, "supplier": profile.supplier_id,
            "fingerprint": profile.fingerprint,
            "profile_hash": profile.profile_sha256,
            "source_hash": profile.approved_source_sha256,
            "reviewer": self.identity.user_id, "document": document,
        })
        found: dict[str, object] = self.connection.execute(text("""
            SELECT profile_json FROM supplier_reviewed_profiles
            WHERE organization_id=:org AND supplier_id=:supplier
              AND schema_fingerprint=:fingerprint AND profile_sha256=:profile_hash
        """), {
            "org": self.organization_id, "supplier": profile.supplier_id,
            "fingerprint": profile.fingerprint, "profile_hash": profile.profile_sha256,
        }).scalar_one()
        if found != document:
            raise ValueError("Immutable PostgreSQL supplier profile content mismatch.")
        return profile

    def load(
        self, supplier_id: str, schema_fingerprint: str,
    ) -> ReviewedSupplierProfile | None:
        authorize_test_tenant_runtime(
            self.connection, self.identity, self.organization_id, "read",
        )
        if (
            not supplier_id or len(supplier_id) > 120
            or not supplier_id.isascii()
            or any(not (char.isalnum() or char in "._-") for char in supplier_id)
            or len(schema_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in schema_fingerprint)
        ):
            raise ValueError("Invalid supplier review selector.")
        rows = self.connection.execute(text("""
            SELECT supplier_id, schema_fingerprint, profile_sha256, profile_json
            FROM supplier_reviewed_profiles
            WHERE organization_id=:org AND supplier_id=:supplier
              AND schema_fingerprint=:fingerprint
            ORDER BY created_at, profile_sha256
            LIMIT 2
        """), {
            "org": self.organization_id, "supplier": supplier_id,
            "fingerprint": schema_fingerprint,
        }).mappings().all()
        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousSupplierProfiles(
                "Multiple approved review versions need an explicit choice."
            )
        row = rows[0]
        profile = _validate_loaded_document(row["profile_json"])
        if (
            profile.organization_id != self.organization_id
            or profile.supplier_id != supplier_id
            or profile.fingerprint != schema_fingerprint
            or profile.profile_sha256 != row["profile_sha256"]
        ):
            raise ValueError("Stored tenant review profile identity mismatch.")
        return profile
