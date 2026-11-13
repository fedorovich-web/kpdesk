from dataclasses import replace
from io import BytesIO

import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from test_commercial_document import content
from test_company_profile import profile

from kpdesk.quotes.appearance import DocumentAppearance, header_text_color
from kpdesk.quotes.pdf import render_draft_pdf


@pytest.mark.parametrize('color', ['#fff', 'red', 'url(https://example.invalid)', '#12345678', '#ZZZZZZ', '#123456\n'])
def test_color_accepts_only_six_hex_digits(color: str) -> None:
    with pytest.raises(ValidationError):
        DocumentAppearance(accent_color=color)


def test_style_preserves_legacy_documents_and_selects_readable_header_text() -> None:
    assert 'appearance' not in content().model_dump(mode='json')
    assert DocumentAppearance(accent_color='#abcdef').accent_color == '#ABCDEF'
    assert header_text_color('#FFFFFF') == '#000000'
    assert header_text_color('#000000') == '#FFFFFF'
    assert header_text_color('#FFFF00') == '#000000'
    with pytest.raises(ValidationError):
        DocumentAppearance.model_validate({'template': 'external-html'})


@pytest.mark.parametrize('template', ['presentation', 'compact'])
def test_both_layouts_render_saved_company_facts_and_keep_financials(template: str) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from test_quote_pdf import draft

    from kpdesk.quotes.document import DocumentApproval

    appearance = DocumentAppearance.model_validate({'template': template, 'accent_color': '#FFFF00'})
    document = content().model_copy(update={'appearance': appearance, 'company_profile': profile()})
    quote = draft()
    approved = replace(quote, approved=True, document=document,
                       approval=DocumentApproval(user_id=uuid4(), approved_at=datetime.now(UTC),
                                                 source_revision=1, source_snapshot_sha256=quote.snapshot_sha256))
    raw = render_draft_pdf(approved)
    text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    assert 'О компании' in text and profile().data.about in text
    assert profile().data.advantages in text
    assert '1399.60' in text and 'Закупка' not in text and 'Наценка' not in text
    assert render_draft_pdf(approved) == raw
    hidden = replace(approved, document=document.model_copy(update={
        'appearance': appearance.model_copy(update={'show_company_section': False}),
    }))
    hidden_text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(render_draft_pdf(hidden))).pages)
    assert 'О компании' not in hidden_text and profile().data.about not in hidden_text
