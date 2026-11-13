"""Sender details survive profile storage, document snapshots and PDF rendering."""
import hashlib
import json
from dataclasses import replace
from io import BytesIO

import pytest
from pypdf import PdfReader
from test_commercial_document import content
from test_company_profile import profile
from test_quote_pdf import draft
from test_recipient_requisites import document_with_recipient, recipient

from kpdesk.company.schema import CompanyProfileData, CompanyProfileSnapshot, profile_digest
from kpdesk.company.storage import CompanyProfileStore
from kpdesk.quotes.document import CommercialDocument
from kpdesk.quotes.narrative import proposal_facts
from kpdesk.quotes.pdf import render_document_preview


def sender_profile() -> CompanyProfileSnapshot:
    data = CompanyProfileData.model_validate({
        **profile().data.model_dump(mode="json"), "issuer_details": recipient().model_dump(mode="json"),
    })
    return CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))


def test_sender_details_roundtrip_through_store_and_document() -> None:
    snap = sender_profile()
    stored = CompanyProfileStore._snapshot({"revision": snap.revision, "data": snap.data.model_dump(mode="json")})
    assert stored == snap
    document = content().model_copy(update={"company_profile": stored})
    assert CommercialDocument.model_validate_json(document.model_dump_json()) == document
    changed = snap.data.model_copy(update={"issuer_details": recipient().model_copy(update={"address": "Другой адрес"})})
    assert profile_digest(changed) != snap.sha256


def test_legacy_profile_hash_and_document_bytes_preserve_shape() -> None:
    snap = profile()
    assert "issuer_details" not in snap.data.model_dump(mode="json")
    raw = json.dumps(snap.data.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    old_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert snap.sha256 == old_digest
    assert old_digest == "cd910bf72a6120964b2cf3df90388beccfc8fdcb77d489624fbf1aaf04a50f29"
    assert CompanyProfileSnapshot.model_validate_json(snap.model_dump_json()).sha256 == old_digest


@pytest.mark.parametrize("with_recipient", [False, True])
def test_pdf_has_distinct_sender_requisites_and_contacts(with_recipient: bool) -> None:
    document = (document_with_recipient() if with_recipient else content()).model_copy(update={"company_profile": sender_profile()})
    raw = render_document_preview(draft(), document)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    assert "Реквизиты отправителя" in text
    assert ("Реквизиты заказчика" in text) is with_recipient
    assert recipient().full_name in text and recipient().inn in text
    address = recipient().address
    assert address is not None and address in text
    assert "sales@example.invalid" in text
    assert "ACTIVE" not in text


def test_sender_identifiers_do_not_reach_writer_or_fact_reviewer() -> None:
    snap = sender_profile()
    text = proposal_facts(replace(draft(), document=content().model_copy(update={"company_profile": snap})),
                          issuer_name=snap.data.display_name, recipient_name="Заказчик",
                          manager_brief="Подготовь предложение", commercial_terms="", company_profile=snap)
    for value in (recipient().inn, recipient().kpp, recipient().ogrn, recipient().address):
        assert value and value not in text
    assert "issuer_details" not in text
