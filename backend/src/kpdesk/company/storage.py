from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.company.schema import CompanyProfileData, CompanyProfileSnapshot, profile_digest
from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.db.tenant_access import TenantOperation


class CompanyProfileConflict(ValueError):
    pass


@dataclass(frozen=True)
class CompanyProfileStore:
    connection: Connection
    organization_id: UUID
    cookie: str
    csrf: str | None = None

    def _authorize(self, operation: TenantOperation) -> None:
        bind_authenticated_tenant(self.connection, session_token=self.cookie,
                                  organization_id=self.organization_id, operation=operation, csrf=self.csrf)

    @staticmethod
    def _snapshot(value: object) -> CompanyProfileSnapshot | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("Invalid company profile store response")
        data = CompanyProfileData.model_validate(value["data"])
        return CompanyProfileSnapshot(revision=value["revision"], data=data, sha256=profile_digest(data))

    def get(self, *, for_update: bool = False) -> CompanyProfileSnapshot | None:
        self._authorize("read")
        if for_update:
            self.connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('kpdesk:company-profile:' || :org,0))"),
                                    {"org": str(self.organization_id)})
        return self._snapshot(self.connection.scalar(text(
            "SELECT public.kpdesk_company_profile_read(:org)"
        ), {"org": self.organization_id}))

    def save(self, data: CompanyProfileData, expected: int) -> CompanyProfileSnapshot:
        self._authorize("manage")
        statement = text("SELECT public.kpdesk_company_profile_save(:org,:data,:expected)").bindparams(
            bindparam("data", type_=JSONB))
        result = self._snapshot(self.connection.scalar(statement, {
            "org": self.organization_id, "data": data.model_dump(mode="json"), "expected": expected,
        }))
        if result is None:
            raise CompanyProfileConflict("stale_company_profile")
        return result
