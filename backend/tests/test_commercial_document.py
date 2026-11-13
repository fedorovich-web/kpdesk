"""A client-facing offer contains a substantive proposal, never internal costs."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from uuid import uuid4

from pypdf import PdfReader
from test_quote_pdf import draft

from kpdesk.quotes.document import CommercialDocument, DocumentApproval, ProposalNarrative
from kpdesk.quotes.pdf import render_draft_pdf


def content() -> CommercialDocument:
    return CommercialDocument(
        issuer_name="Учебная компания", recipient_name="Учебный заказчик",
        issued_on=date(2026, 10, 28),
        manager_brief="Предложи решение для обеспечения офиса бумагой, деловой стиль.",
        commercial_terms="Срок действия предложения: до 30.10.2026. Доставка согласуется отдельно.",
        narrative=ProposalNarrative(
            headline="Комплектация офиса расходными материалами",
            opening="Уважаемые коллеги! Предлагаем решение по обеспечению вашего офиса бумагой.",
            solution="В состав предложения включена офисная бумага формата А4 плотностью 80 г/м². "
                     "Подбор подготовлен по вашей заявке; состав и стоимость приведены ниже.",
            closing="Будем рады обсудить состав заказа и согласовать условия поставки.",
        ),
    )


def test_approved_pdf_is_a_textual_commercial_offer_and_hides_procurement() -> None:
    original = draft()
    quote = replace(
        original, approved=True, document=content(),
        approval=DocumentApproval(
            user_id=uuid4(), approved_at=datetime.now(UTC), source_revision=1,
            source_snapshot_sha256=original.snapshot_sha256,
        ),
    )
    raw = render_draft_pdf(quote)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    assert "Учебная компания" in text and "Учебный заказчик" in text
    assert "Уважаемые коллеги!" in text
    assert "Комплектация офиса" in text
    assert "согласовать условия поставки" in text
    assert "30.10.2026" in text and "1399.60" in text
    assert "Закупка" not in text and "Наценка" not in text
    assert "Не утверждено" not in text and "Черновик" not in text
