"""Manager-reviewed company data survives universal snapshots, never LLM facts."""
import json
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from kpdesk.api.universal_routes import QuoteBody
from kpdesk.company.requisites import PartyDetails
from kpdesk.universal.document import narrative_facts, render_pdf
from kpdesk.universal.matching import build_quote
from kpdesk.universal.schema import (
    CatalogSnapshot,
    QuoteContext,
    QuoteSnapshot,
    RequestLine,
    Selection,
    UniversalProduct,
    UniversalRequest,
)
from kpdesk.universal.storage import _parse_quote


def party(*, recipient: bool = False) -> PartyDetails:
    return PartyDetails(
        short_name="Учебный заказчик" if recipient else "Учебный поставщик",
        full_name='ООО «Учебный заказчик»' if recipient else 'ООО «Учебный поставщик»',
        inn="9900000017" if recipient else "9900000000",
        kpp="990002001" if recipient else "990001001",
        ogrn="1269900000027" if recipient else "1269900000016",
        address="Учебный адрес заказчика" if recipient else "Учебный адрес поставщика",
        status="ACTIVE",
    )


def quote(context: QuoteContext) -> QuoteSnapshot:
    catalog = CatalogSnapshot(version_id=uuid4(), source_sha256="b" * 64, products=(
        UniversalProduct(sku="P", name="Учебный насос", sale_unit="шт", price_rub=Decimal("123.45")),))
    request = UniversalRequest(lines=(RequestLine(description="Насос", source_quote="Насос 2 шт",
        quantity=Decimal(2), sale_unit="шт"),))
    return build_quote(catalog, request, (Selection(line_number=1, sku="P"),), context, uuid4(), 1)


def context() -> QuoteContext:
    return QuoteContext(issuer_name=party().short_name, recipient_name=party(recipient=True).short_name,
        issuer_details=party(), recipient_details=party(recipient=True), issuer_contacts="sales@example.invalid")


def test_optional_fields_preserve_old_payload_and_snapshot_hash() -> None:
    original = quote(QuoteContext(issuer_name="Компания"))
    payload = original.model_dump(mode="json")
    for field in ("issuer_details", "recipient_details", "issuer_contacts"):
        assert field not in payload
    assert QuoteSnapshot.model_validate(payload).model_dump(mode="json") == payload
    assert _parse_quote(payload, original.snapshot_sha256, original.quote_id,
        original.revision, original.catalog_version_id) == original


def test_api_forwards_reviewed_parties_and_contacts_into_context() -> None:
    data = context()
    original = quote(data)
    body = QuoteBody.model_validate({"catalog_version_id": original.catalog_version_id,
        "request": original.source_request, "selections": list(original.selections),
        **data.model_dump(exclude={"narrative"})})
    assert body.context() == data


def test_saved_parties_render_in_pdf_and_are_excluded_from_llm_facts() -> None:
    saved = quote(context())
    restored = _parse_quote(saved.model_dump(mode="json"), saved.snapshot_sha256,
        saved.quote_id, saved.revision, saved.catalog_version_id)
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(restored))).pages)
    for expected in ("Реквизиты отправителя", "Реквизиты заказчика", party().inn,
                     party(recipient=True).inn, "990001001", "1269900000016",
                     "Учебный адрес поставщика", "sales@example.invalid", "246,90"):
        assert expected in text
    facts = narrative_facts(restored)
    for sensitive in (party().inn, party(recipient=True).inn, party().kpp,
                      party().ogrn, party().address, "sales@example.invalid"):
        assert sensitive is not None and sensitive not in facts
    assert "issuer_details" not in json.loads(facts)


def test_tampered_stored_requisites_fail_snapshot_integrity() -> None:
    saved = quote(context())
    body = saved.model_dump(mode="json")
    body["issuer_details"]["address"] = "Подменённый адрес"
    with pytest.raises(ValidationError, match="snapshot_digest_mismatch"):
        QuoteSnapshot.model_validate(body)


def test_recipient_name_must_match_its_reviewed_party() -> None:
    with pytest.raises(ValidationError, match="recipient_requisites_mismatch"):
        QuoteContext(issuer_name="Компания", recipient_name="Другая компания",
            recipient_details=party(recipient=True))


def test_contacts_are_bounded_and_null_fields_do_not_change_old_api_payload() -> None:
    with pytest.raises(ValidationError):
        QuoteContext(issuer_name="Компания", issuer_contacts="x" * 601)
    body = QuoteBody.model_validate({"catalog_version_id": uuid4(),
        "request": {"lines": [{"description": "Насос", "source_quote": "Насос"}]},
        "selections": [{"line_number": 1, "sku": "P"}], "issuer_name": "Компания",
        "issuer_details": None, "recipient_details": None, "issuer_contacts": None})
    assert not {"issuer_details", "recipient_details", "issuer_contacts"} & body.model_dump().keys()


def test_contacts_with_null_byte_are_rejected_before_sql_storage() -> None:
    with pytest.raises(ValidationError, match="blank_or_nul_value"):
        QuoteContext(issuer_name="Компания", issuer_contacts="sales\x00@example.invalid")


def test_pdf_escapes_party_markup_and_preserves_details_across_multiple_pages() -> None:
    sender = PartyDetails.model_validate({**party().model_dump(),
        "full_name": 'ООО <Учебный & поставщик>', "address": "Учебный адрес " * 30})
    data = QuoteContext.model_validate({**context().model_dump(), "issuer_details": sender})
    saved = quote(data)
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(saved))).pages)
    assert 'ООО <Учебный & поставщик>' in text and party().inn in text
    assert "sales@example.invalid" in text and party(recipient=True).inn in text
