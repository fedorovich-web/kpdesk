import json

import pytest
from pydantic import ValidationError
from test_quote_pdf import draft

from kpdesk.company.schema import CompanyProfileData, CompanyProfileSnapshot, profile_digest
from kpdesk.quotes.narrative import proposal_facts


def profile() -> CompanyProfileSnapshot:
    data = CompanyProfileData(display_name="Учебная компания", about="Поставка офисных товаров.",
                              advantages="Помогаем подобрать состав по заявке.",
                              contacts="sales@example.invalid", default_terms="Условия согласуются.",
                              generation_style="Кратко, деловой стиль.")
    return CompanyProfileSnapshot(revision=1, data=data, sha256=profile_digest(data))


def test_profile_is_bounded_normalized_and_rejects_unverified_shape() -> None:
    assert CompanyProfileData(display_name="  Компания  ").display_name == "Компания"
    for changes in ({"display_name": " "}, {"about": "a" * 1001}, {"contacts": "a\x00b"}, {"price": "0"}):
        with pytest.raises(ValidationError):
            CompanyProfileData.model_validate({"display_name": "Компания", **changes})


def test_profile_snapshot_hash_covers_data() -> None:
    snap = profile()
    with pytest.raises(ValidationError):
        CompanyProfileSnapshot(revision=1, data=snap.data, sha256="a" * 64)
    assert profile_digest(snap.data.model_copy(update={"about": "Другие сведения"})) != snap.sha256


def test_ai_context_uses_company_facts_but_keeps_contacts_outside_provider() -> None:
    snap = profile()
    text = proposal_facts(draft(), issuer_name=snap.data.display_name, recipient_name="Заказчик",
                          manager_brief="Подготовь предложение.", commercial_terms="",
                          company_profile=snap)
    facts = json.loads(text)
    assert facts["company_facts"]["about"] == snap.data.about
    assert facts["company_facts"]["advantages"] == snap.data.advantages
    assert facts["generation_style"] == snap.data.generation_style
    assert snap.data.contacts not in text


def test_saved_document_pdf_uses_its_snapshot_contacts() -> None:
    from dataclasses import replace
    from datetime import UTC, datetime
    from io import BytesIO
    from uuid import uuid4

    from pypdf import PdfReader
    from test_commercial_document import content

    from kpdesk.quotes.document import DocumentApproval
    from kpdesk.quotes.pdf import render_draft_pdf

    original = draft()
    document = content().model_copy(update={"company_profile": profile()})
    saved = replace(original, revision=2, approved=True, document=document,
                    approval=DocumentApproval(user_id=uuid4(), approved_at=datetime.now(UTC),
                                              source_revision=1, source_snapshot_sha256=original.snapshot_sha256))
    before = render_draft_pdf(saved)
    assert "sales@example.invalid" in "\n".join(p.extract_text() for p in PdfReader(BytesIO(before)).pages)
    changed = profile().data.model_copy(update={"contacts": "new@example.invalid"})
    assert saved.document is not None and saved.document.company_profile is not None
    assert changed.contacts != saved.document.company_profile.data.contacts
    assert render_draft_pdf(saved) == before


@pytest.mark.parametrize(("same", "observed_absence"), [(True, False), (False, False), (False, True)])
def test_approval_checks_current_profile_and_locks_it(same: bool, observed_absence: bool) -> None:
    from typing import cast
    from unittest.mock import Mock, patch
    from uuid import uuid4

    from sqlalchemy import Connection
    from test_commercial_document import content

    from kpdesk.company.storage import CompanyProfileStore
    from kpdesk.db.tenant_access import AuthenticatedUser
    from kpdesk.quotes.storage import QuoteConflict
    from kpdesk.quotes.tenant_storage import TenantQuoteRepository

    current = draft()
    conn = Mock(); conn.scalar.return_value = current.revision; conn.execute.return_value.rowcount = 1
    store = TenantQuoteRepository(cast(Connection, conn), AuthenticatedUser(uuid4()), uuid4(), "session", "csrf")
    document = content().model_copy(update={"company_profile": None if observed_absence else profile()})
    with (patch.object(TenantQuoteRepository, "_authorize"), patch.object(TenantQuoteRepository, "_lock_tenant"),
          patch.object(TenantQuoteRepository, "_existing", return_value=None),
          patch.object(TenantQuoteRepository, "_read_revision", return_value=current),
          patch.object(TenantQuoteRepository, "_append", return_value=current) as append,
          patch.object(CompanyProfileStore, "get", return_value=profile() if same or observed_absence else None) as read):
        if same:
            store.approve_document(current.quote_id, current.revision, current.snapshot_sha256, document, "profile-approval")
            assert append.call_args.kwargs["snapshot"].document.company_profile == profile()
        else:
            with pytest.raises(QuoteConflict, match="stale_company_profile"):
                store.approve_document(current.quote_id, current.revision, current.snapshot_sha256, document, "profile-approval")
            append.assert_not_called()
        read.assert_called_once_with(for_update=True)
