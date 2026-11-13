from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from test_commercial_document import content
from test_quote_pdf import draft

from kpdesk.api.tenant_document_routes import ApproveDocumentBody, DocumentPreviewBody
from kpdesk.company.requisites import PartyDetails
from kpdesk.quotes.document import CommercialDocument, DocumentApproval
from kpdesk.quotes.narrative import proposal_facts
from kpdesk.quotes.pdf import render_document_preview, render_draft_pdf


def recipient() -> PartyDetails:
    return PartyDetails(
        full_name='ООО <b>Ромашка</b> & партнёры', short_name='ООО «Ромашка»',
        inn='7707083893', kpp='773601001', ogrn='1027700132195',
        address='Москва, ул. <i>Учебная</i>, дом 7 & корпус 2', status='ACTIVE',
    )


def document_with_recipient() -> CommercialDocument:
    data = content().model_dump(mode='json')
    data.update(recipient_name=recipient().short_name, recipient_details=recipient().model_dump())
    return CommercialDocument.model_validate(data)


@pytest.mark.parametrize('name', ['ООО «Ромашка»', 'ООО <b>Ромашка</b> & партнёры'])
def test_document_accepts_only_exact_full_or_short_recipient_name(name: str) -> None:
    data = document_with_recipient().model_dump(mode='json')
    data['recipient_name'] = name
    assert CommercialDocument.model_validate(data).recipient_name == name


@pytest.mark.parametrize('name', ['Другая организация', '', 'ООО «Ромашка» '])
def test_approval_and_preview_dto_reject_name_requisites_mismatch(name: str) -> None:
    data = document_with_recipient().model_dump(mode='json')
    data['recipient_name'] = name
    base = {'expected_revision': 2, 'source_snapshot_sha256': 'a' * 64, 'document': data}
    with pytest.raises(ValidationError, match='recipient_requisites_mismatch'):
        DocumentPreviewBody.model_validate(base)
    with pytest.raises(ValidationError, match='recipient_requisites_mismatch'):
        ApproveDocumentBody.model_validate({**base, 'reviewed': True})


def test_legacy_document_without_requisites_preserves_payload_shape() -> None:
    assert 'recipient_details' not in content().model_dump(mode='json')
    assert content().recipient_details is None


@pytest.mark.parametrize('preview', [True, False])
def test_pdf_shows_escaped_requisites_as_literal_text(preview: bool) -> None:
    quote = draft()
    document = document_with_recipient()
    if preview:
        raw = render_document_preview(quote, document)
    else:
        approved = replace(quote, revision=quote.revision + 1, approved=True, document=document,
                           approval=DocumentApproval(user_id=uuid4(), approved_at=datetime.now(UTC),
                                                     source_revision=quote.revision,
                                                     source_snapshot_sha256=quote.snapshot_sha256))
        raw = render_draft_pdf(approved)
    text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    for expected in ('Реквизиты заказчика', recipient().full_name, recipient().inn,
                     recipient().kpp, recipient().ogrn, recipient().address):
        assert expected is not None and expected in text
    assert 'ACTIVE' not in text
    assert 'Закупка' not in text
    assert quote.document is None and not quote.approved


def test_recipient_tax_fields_are_not_sent_to_proposal_facts() -> None:
    document = document_with_recipient()
    quote = replace(draft(), document=document)
    facts = proposal_facts(
        quote, issuer_name=document.issuer_name, recipient_name=document.recipient_name,
        manager_brief=document.manager_brief, commercial_terms=document.commercial_terms,
    )
    parsed = json.loads(facts)
    assert parsed['recipient'] == recipient().short_name
    for private in (recipient().inn, recipient().kpp, recipient().ogrn, recipient().address):
        assert private is not None and private not in facts
    assert 'recipient_details' not in facts and 'kpp' not in facts and 'ogrn' not in facts


def test_requisites_reject_invalid_tax_identifiers_and_unknown_private_fields() -> None:
    for changes in ({'inn': '7707083894'}, {'kpp': '123'}, {'ogrn': '123'},
                    {'founders': ['Частное лицо']}, {'director': 'Частное лицо'}):
        with pytest.raises(ValidationError):
            PartyDetails.model_validate({**recipient().model_dump(), **changes})
