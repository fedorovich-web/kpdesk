"""New branded layouts must retain facts, financials, provenance and pagination."""
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from typing import cast

import pymupdf
import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.generic import DictionaryObject
from test_pdf_pagination import proposal
from test_recipient_requisites import recipient

from kpdesk.company.requisites import PartyDetails
from kpdesk.company.schema import CompanyProfileData, CompanyProfileSnapshot, profile_digest
from kpdesk.quotes.appearance import DocumentAppearance
from kpdesk.quotes.document import CommercialDocument
from kpdesk.quotes.pdf import QuotePDFError, render_document_preview
from kpdesk.quotes.storage import StoredQuote


def branded_proposal(template: str = "presentation", *, parties: bool = True) -> tuple[StoredQuote, CommercialDocument]:
    quote, document = proposal()
    assert document.appearance is not None
    appearance = DocumentAppearance.model_validate({**document.appearance.model_dump(mode="json"),
                                                   "layout_version": 2, "template": template,
                                                   "accent_color": "#008B50"})
    document = document.model_copy(update={"appearance": appearance})
    if parties:
        data = CompanyProfileData(display_name="Учебная поставка", contacts="sales@example.invalid",
                                  issuer_details=PartyDetails(full_name='ООО «Учебная поставка»',
                                                              short_name="Учебная поставка", inn="7701234560",
                                                              address="Учебный адрес поставщика"))
        snap = CompanyProfileSnapshot(revision=1, data=data, sha256=profile_digest(data))
        document = document.model_copy(update={"issuer_name": data.display_name, "company_profile": snap,
                                               "recipient_name": recipient().short_name, "recipient_details": recipient()})
    return quote, document


def test_unversioned_appearance_keeps_original_json_shape() -> None:
    value = DocumentAppearance()
    assert value.layout_version == 1
    assert value.model_dump(mode="json") == {"template": "presentation", "accent_color": "#007F5F", "show_company_section": True}
    for invalid in (0, 3, True, "2", 2.0):
        with pytest.raises(ValidationError):
            DocumentAppearance.model_validate({"layout_version": invalid})


@pytest.mark.parametrize("template", ["presentation", "compact"])
def test_new_layout_embeds_inter_and_keeps_complete_short_offer(template: str) -> None:
    quote, document = branded_proposal(template)
    raw = render_document_preview(quote, document)
    reader = PdfReader(BytesIO(raw))
    assert len(reader.pages) == 1, "A short offer including both parties must stay together."
    text = " ".join(reader.pages[0].extract_text().split())
    for expected in ("Реквизиты отправителя", "Реквизиты заказчика", "7701234560", recipient().inn,
                     "sales@example.invalid", "1 128,50", "699,80", "ПРЕДПРОСМОТР", "НЕ УТВЕРЖДЕНО"):
        assert expected in text
    assert "Закупка" not in text and "ACTIVE" not in text
    resources = cast(DictionaryObject, reader.pages[0]["/Resources"].get_object())
    font_map = cast(DictionaryObject, resources["/Font"].get_object())
    fonts = [cast(DictionaryObject, font.get_object()) for font in font_map.values()]
    inter = [font for font in fonts if "Inter" in str(font.get("/BaseFont"))]
    assert len(inter) >= 2
    assert all("/FontFile2" in cast(DictionaryObject, font["/FontDescriptor"].get_object()) for font in inter)
    assert render_document_preview(quote, document) == raw


@pytest.mark.parametrize("template", ["presentation", "compact"])
def test_new_layout_long_table_repeats_header_and_preserves_every_row(template: str) -> None:
    quote, document = branded_proposal(template)
    rows = tuple(quote.lines[0].model_copy(update={"line_number": i, "sku": f"BR-{i:03}",
                  "product_name": f"Позиция {i}: бумага для офисной печати А4 80 г/м², 500 листов"}) for i in range(1, 21))
    quote = replace(quote, lines=rows, total_rub=Decimal("699.80") * 20)
    raw = render_document_preview(quote, document)
    reader = PdfReader(BytesIO(raw))
    text = " ".join(" ".join(page.extract_text().split()) for page in reader.pages)
    assert "BR-001" in reader.pages[0].extract_text()
    for i in range(1, 21):
        assert f"BR-{i:03}" in text
    assert text.count("Наименование") >= 2 and "13 996,00" in text
    assert "Реквизиты отправителя" in text and "Реквизиты заказчика" in text
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:  # type: ignore[no-untyped-call]
        for page in pdf:
            for x0, y0, x1, y1, word, *_ in page.get_text("words"):
                assert x0 >= 35 and x1 <= page.rect.width - 35, word
                assert y0 >= 8 and y1 <= page.rect.height - 20, word


def test_new_layout_wraps_maximum_legal_names_and_addresses_without_losing_text() -> None:
    quote, document = branded_proposal()
    assert document.company_profile is not None and document.company_profile.data.issuer_details is not None
    party = document.company_profile.data.issuer_details.model_copy(update={"full_name": "А" * 500, "address": "Б" * 500})
    data = document.company_profile.data.model_copy(update={"issuer_details": party})
    snap = CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))
    raw = render_document_preview(quote, document.model_copy(update={"company_profile": snap}))
    text = "".join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    assert text.count("А") >= 500 and text.count("Б") >= 500


@pytest.mark.parametrize("template", ["presentation", "compact"])
@pytest.mark.parametrize("logo_kind", ["default", "wide", "portrait", "none"])
def test_header_keeps_short_company_name_on_one_line(template: str, logo_kind: str) -> None:
    from test_company_logo import source_logo

    from kpdesk.company.logo import prepare_logo

    quote, document = branded_proposal(template)
    assert document.company_profile is not None and document.appearance is not None
    data = document.company_profile.data
    if logo_kind in {"wide", "portrait"}:
        data = data.model_copy(update={"logo": prepare_logo(source_logo(
            (800, 160) if logo_kind == "wide" else (120, 400)))})
    snapshot = CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))
    document = document.model_copy(update={
        "issuer_name": 'ООО «Учебный поставщик»', "company_profile": snapshot,
        "appearance": document.appearance.model_copy(update={
            "default_logo": "kpdesk_v1" if logo_kind == "default" else None}),
    })
    with pymupdf.open(stream=render_document_preview(quote, document), filetype="pdf") as pdf:  # type: ignore[no-untyped-call]
        matches = pdf[0].search_for(document.issuer_name)
        assert len(matches) == 1, "A short company name must fit beside the logo on one line."
        metadata = pdf[0].search_for("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ")
        assert len(metadata) == 1 and matches[0].x1 + 12 <= metadata[0].x0


@pytest.mark.parametrize("template", ["presentation", "compact"])
def test_header_wraps_long_company_name_without_overlap_or_text_loss(template: str) -> None:
    quote, document = branded_proposal(template, parties=False)
    assert document.appearance is not None
    document = document.model_copy(update={
        "issuer_name": 'ООО «Учебный поставщик офисных принадлежностей и расходных материалов»',
        "appearance": document.appearance.model_copy(update={"default_logo": "kpdesk_v1"}),
    })
    with pymupdf.open(stream=render_document_preview(quote, document), filetype="pdf") as pdf:  # type: ignore[no-untyped-call]
        page = pdf[0]
        matches = page.search_for(document.issuer_name)
        assert len(matches) > 1
        metadata = page.search_for("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ")[0]
        recipient = page.search_for("Для:")[0]
        assert all(rect.x1 + 12 <= metadata.x0 and rect.y1 < recipient.y0 for rect in matches)
        assert " ".join(document.issuer_name.split()) in " ".join(page.get_text().split())


def test_new_layout_uses_packaged_fonts_and_does_not_require_operator_arial(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from kpdesk.quotes import pdf

    monkeypatch.setattr(pdf, "_FONT", Path("missing-legacy-regular.ttf"))
    monkeypatch.setattr(pdf, "_FONT_BOLD", Path("missing-legacy-bold.ttf"))
    quote, document = branded_proposal()
    assert render_document_preview(quote, document).startswith(b"%PDF-")
    monkeypatch.setattr(pdf, "FONT_DIRECTORY", Path("missing-brand-assets"))
    with pytest.raises(QuotePDFError, match="brand_font_missing"):
        render_document_preview(quote, document)


def test_contacts_with_many_line_breaks_can_continue_on_the_next_page() -> None:
    quote, document = branded_proposal()
    assert document.company_profile is not None
    data = document.company_profile.data.model_copy(update={"contacts": "\n".join(f"Контакт{i}" for i in range(60))})
    assert len(data.contacts) <= 600
    snap = CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))
    raw = render_document_preview(quote, document.model_copy(update={"company_profile": snap}))
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    assert "Контакт0" in text and "Контакт59" in text


def test_new_layout_keeps_alternative_totals_separate_and_formats_the_difference() -> None:
    from test_document_alternatives import saved

    from kpdesk.quotes.alternatives import resolve_alternatives
    from kpdesk.quotes.document import AlternativeSelection
    from kpdesk.quotes.line import ManagerSkuSelection

    quote, products = saved()
    alternative = resolve_alternatives(quote, products, [AlternativeSelection(
        kind="lower_cost", selections=(ManagerSkuSelection(line_number=1, sku="SECOND"),))])[0]
    _, document = branded_proposal()
    document = document.model_copy(update={"alternatives": (alternative,)})
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(render_document_preview(quote, document))).pages)
    for expected in ("100,10", "60,00", "40,10", "FIRST", "SECOND", "Их суммы не складываются"):
        assert expected in text
    assert "160,10" not in text
