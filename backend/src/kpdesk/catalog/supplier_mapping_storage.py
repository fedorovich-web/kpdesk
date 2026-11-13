"""Tenant-scoped append-only PostgreSQL persistence for reviewed supplier layout maps."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Connection, text

from kpdesk.catalog.supplier_inference import SourceInspection
from kpdesk.catalog.supplier_profiles import (
    PriceBasis,
    ReviewedSupplierProfile,
    _profile_checksum,
    schema_fingerprint,
)
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import AuthenticatedUser, TenantAccessDenied

_SAVE = text("SELECT public.kpdesk_supplier_mapping_save(:org,:supplier,CAST(:data AS jsonb))")
_LATEST = text("""
    SELECT profile FROM public.kpdesk_supplier_mapping_latest(:org,:supplier,:fingerprint)
""")
_HISTORY = text("""
    SELECT profile,reviewed_at
    FROM public.kpdesk_supplier_mapping_history(:org,:supplier,:limit,:offset)
""")
_HISTORY_SIGNATURE = "public.kpdesk_supplier_mapping_history(uuid,text,integer,integer)"
_SIGNATURE = (
    "public.kpdesk_supplier_mapping_save(uuid,text,jsonb)"
)
_SUPPLIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def profile_payload(profile: ReviewedSupplierProfile) -> dict[str, object]:
    data: dict[str, object] = asdict(profile)
    data["organization_id"] = str(profile.organization_id)
    data["approved_by"] = str(profile.approved_by)
    data["columns"] = [[key, value] for key, value in profile.columns]
    return data


def parse_profile_payload(data: object) -> ReviewedSupplierProfile:
    if not isinstance(data, dict):
        raise TypeError("Supplier profile JSON object expected.")
    required = set(ReviewedSupplierProfile.__dataclass_fields__)
    if set(data) != required:
        raise ValueError("Supplier profile fields changed or missing.")
    try:
        raw_columns = data["columns"]
        if not isinstance(raw_columns, list) or not 2 <= len(raw_columns) <= 7:
            raise ValueError("Invalid saved profile columns.")
        columns: list[tuple[str, int | str]] = []
        for pair in raw_columns:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Invalid saved mapping pair.")
            key, value = pair
            if not isinstance(key, str) or type(value) not in (str, int):
                raise ValueError("Invalid saved mapping entry.")
            columns.append((key, value))
        if tuple(columns) != tuple(sorted(columns)) or len(set(columns)) != len(columns):
            raise ValueError("Noncanonical stored columns.")
        org = uuid.UUID(cast(str, data["organization_id"]))
        actor = uuid.UUID(cast(str, data["approved_by"]))
        basis = cast(PriceBasis, data["price_basis"])
        profile = ReviewedSupplierProfile(
            organization_id=org,
            supplier_id=cast(str, data["supplier_id"]),
            fingerprint=cast(str, data["fingerprint"]),
            source_format=cast(str, data["source_format"]),
            columns=tuple(columns),
            header_row=cast(int | None, data["header_row"]),
            sheet=cast(str | None, data["sheet"]),
            delimiter=cast(str | None, data["delimiter"]),
            encoding=cast(str | None, data["encoding"]),
            items_key=cast(str | None, data["items_key"]),
            confirmed_currency=cast(Any, data["confirmed_currency"]),
            price_basis=basis,
            approved_by=actor,
            approved_source_sha256=cast(str, data["approved_source_sha256"]),
            review_evidence=cast(str, data["review_evidence"]),
            profile_sha256=cast(str, data["profile_sha256"]),
        )
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("Stored supplier profile is invalid.") from error
    check = profile_payload(profile)
    if data != check:
        raise ValueError("Stored supplier profile types invalid.")
    checksum_data = {key: value for key, value in check.items() if key != "profile_sha256"}
    if (_SHA.fullmatch(profile.profile_sha256) is None
            or _SHA.fullmatch(profile.fingerprint) is None
            or _SHA.fullmatch(profile.approved_source_sha256) is None
            or _profile_checksum(checksum_data) != profile.profile_sha256):
        raise ValueError("Stored supplier mapping checksum mismatch.")
    return profile


@dataclass(frozen=True, slots=True)
class SupplierMappingHistoryRecord:
    profile: ReviewedSupplierProfile
    reviewed_at: datetime


@dataclass(frozen=True, slots=True)
class TenantSupplierMappingStore:
    connection: Connection
    identity: AuthenticatedUser
    organization_id: uuid.UUID
    session_token: str
    csrf_token: str = ""

    def _authorize(self, *, write: bool) -> None:
        scope = bind_authenticated_tenant(
            self.connection, session_token=self.session_token,
            organization_id=self.organization_id,
            operation="write" if write else "read",
            csrf=self.csrf_token if write else None,
        )
        if scope.identity.user_id != self.identity.user_id:
            self.connection.execute(text("""
                SELECT set_config('kpdesk.session_sha256','',true),
                       set_config('kpdesk.organization_id','',true)
            """))
            raise TenantAccessDenied("Supplier map reviewer session mismatch.")
        if self.connection.scalar(text("""
            SELECT to_regprocedure(:signature) IS NOT NULL
        """), {"signature": _SIGNATURE}) is not True:
            raise RuntimeError("Supplier mapping migration 0022 unavailable.")

    def save(self, profile: ReviewedSupplierProfile) -> None:
        if (profile.organization_id != self.organization_id
                or profile.approved_by != self.identity.user_id
                or _SUPPLIER.fullmatch(profile.supplier_id) is None):
            raise TenantAccessDenied("Supplier mapping profile scope mismatch.")
        payload = profile_payload(profile)
        parse_profile_payload(payload)
        self._authorize(write=True)
        accepted = self.connection.scalar(_SAVE, {
            "org": self.organization_id, "supplier": profile.supplier_id,
            "data": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        })
        if accepted is not True:
            raise TenantAccessDenied("SQL refused the supplier mapping approval.")

    def latest(
        self, supplier_id: str, inspection: SourceInspection,
    ) -> ReviewedSupplierProfile | None:
        if _SUPPLIER.fullmatch(supplier_id) is None:
            raise ValueError("Invalid supplier selector.")
        fingerprint = schema_fingerprint(inspection)
        self._authorize(write=False)
        payload = self.connection.scalar(_LATEST, {
            "org": self.organization_id,
            "supplier": supplier_id, "fingerprint": fingerprint,
        })
        if payload is None:
            return None
        profile = parse_profile_payload(payload)
        if (profile.organization_id != self.organization_id
                or profile.supplier_id != supplier_id
                or profile.fingerprint != fingerprint):
            raise ValueError("Stored mapping scope or fingerprint mismatch.")
        return profile

    def history(
        self, supplier_id: str, *, limit: int = 20, offset: int = 0,
    ) -> list[SupplierMappingHistoryRecord]:
        if _SUPPLIER.fullmatch(supplier_id) is None:
            raise ValueError("Invalid supplier selector.")
        if not 1 <= limit <= 50 or not 0 <= offset <= 500:
            raise ValueError("Invalid bounded history pagination.")
        self._authorize(write=False)
        if self.connection.scalar(text("""
            SELECT to_regprocedure(:signature) IS NOT NULL
        """), {"signature": _HISTORY_SIGNATURE}) is not True:
            raise RuntimeError("Supplier mapping migration 0023 unavailable.")
        rows = self.connection.execute(_HISTORY, {
            "org": self.organization_id, "supplier": supplier_id,
            "limit": limit, "offset": offset,
        })
        history: list[SupplierMappingHistoryRecord] = []
        for payload, reviewed_at in rows:
            profile = parse_profile_payload(payload)
            if profile.organization_id != self.organization_id or profile.supplier_id != supplier_id:
                raise ValueError("Stored mapping history scope mismatch.")
            if not isinstance(reviewed_at, datetime):
                raise TypeError("Invalid stored mapping review timestamp.")
            history.append(SupplierMappingHistoryRecord(profile, reviewed_at))
        return history
