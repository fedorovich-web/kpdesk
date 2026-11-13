"""Cyrillic quotation PDF is generated only from persisted complete revisions."""

from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from pypdf import PdfReader

from kpdesk.quotes.pdf import QuotePDFError, render_draft_pdf
from kpdesk.quotes.storage import LockedQuoteLine, StoredQuote


def draft(*, complete: bool = True) -> StoredQuote:
    lines = tuple(
        LockedQuoteLine(
            line_number=i + 1,
            description="Бумага для принтера А4 80 г/м², 500 листов — офисная",
            status="auto_selected" if complete else "needs_review",
            sku=f"PAPER-{i+1:04}",
            product_name="Бумага офисная А4 80 г/м²",
            requested_quantity=2,
            requested_unit="пачка",
            sale_quantity=2,
            sale_unit="пачка",
            unit_price_rub=Decimal("349.90") if complete else None,
            line_total_rub=Decimal("699.80") if complete else None,
            reason_codes=(),
        ) for i in range(2)
    )
    return StoredQuote(
        quote_id=uuid4(), revision=2, catalog_version_id=uuid4(),
        ready_for_draft=complete, total_rub=Decimal("1399.60") if complete else None,
        lines=lines, snapshot_sha256="a" * 64, approved=False,
    )


def test_pdf_cyrillic_and_totals_are_readable() -> None:
    quote = draft()
    raw = render_draft_pdf(quote)
    assert raw.startswith(b"%PDF-")
    assert len(raw) > 2000
    reader = PdfReader(BytesIO(raw))
    assert len(reader.pages) >= 1
    extracted = "\n".join(page.extract_text() for page in reader.pages)
    assert "Бумага офисная" in extracted
    assert "1399.60" in extracted
    assert str(quote.quote_id) in extracted
    assert "Черновик" in extracted
    assert "Не утверждено" in extracted
    assert "НДС, скидки" in extracted  # explicitly unspecified, never fabricated


def test_unresolved_draft_cannot_be_exported_as_complete_kp() -> None:
    with pytest.raises(QuotePDFError, match="incomplete_draft"):
        render_draft_pdf(draft(complete=False))


def test_render_is_deterministic_without_external_assets() -> None:
    quote = draft()
    assert render_draft_pdf(quote) == render_draft_pdf(quote)


def test_no_disallowed_font_path_can_be_injected() -> None:
    with pytest.raises(QuotePDFError, match="font_not_allowed"):
        render_draft_pdf(draft(), font_path=Path("C:/temp/arbitrary.ttf"))


def test_pdf_twenty_long_item_rows_wrap_and_keep_text_in_page_bounds() -> None:
    from dataclasses import replace

    import pymupdf

    original=draft()
    source=original.lines[0]
    lines=tuple(source.model_copy(update={
        "line_number": i,
        "sku": f"P-{i:04}",
        "product_name": (
            "Бумага офисная для цветной лазерной печати "
            "А4, плотность 80 г/м², белизна 146% CIE, 500 листов"
        ),
    }) for i in range(1,21))
    quote=replace(original,lines=lines,total_rub=Decimal("13996.00"))
    document=pymupdf.open(stream=render_draft_pdf(quote),filetype="pdf")  # type: ignore[no-untyped-call]
    assert len(document)>=2
    pages=[document.load_page(index) for index in range(len(document))]  # type: ignore[no-untyped-call]
    text="\n".join(page.get_text() for page in pages)
    assert "P-0001" in text
    assert "P-0020" in text
    assert text.count("Наименование")>=2  # long table repeats column headers
    for page in pages:
        assert all(
            block[0]>=-1 and block[1]>=-1
            and block[2]<=page.rect.width+1
            and block[3]<=page.rect.height+1
            for block in page.get_text("blocks")
        )
    document.close()  # type: ignore[no-untyped-call]
