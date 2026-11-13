"""Brand-consistent, visibly unapproved PDF from immutable universal facts."""
from __future__ import annotations

import json
from html import escape
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Flowable, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from kpdesk.company.default_logo import kpdesk_default_logo
from kpdesk.quotes.pdf_brand import (
    BORDER,
    CONTENT_WIDTH,
    INK,
    MUTED,
    SURFACE,
    TEXT,
    money,
    party_panels,
    proposal_header,
    register_brand_fonts,
    total_panel,
)
from kpdesk.universal.schema import QuoteLine, QuoteSnapshot
from kpdesk.universal.units import normalized


def _verified_specs(line: QuoteLine) -> dict[str, str]:
    counts: dict[str, int] = {}
    for spec in line.specifications:
        key = normalized(spec.name)
        counts[key] = counts.get(key, 0) + 1
    verified = {
        spec.name: str(spec.value) + (" " + spec.unit if spec.unit else "")
        for spec in line.specifications
        if counts[normalized(spec.name)] == 1 and normalized(spec.name) not in {"category", "brand", "model"}
    }
    for field in ("category", "model"):
        value = getattr(line, field)
        if value is not None:
            verified[field] = value
    return verified


def narrative_facts(quote: QuoteSnapshot) -> str:
    if not quote.ready_for_draft:
        raise ValueError("incomplete_draft")
    payload = {
        "facts_version": "kpdesk_universal_proposal_facts_v1",
        "issuer": quote.issuer_name, "recipient": quote.recipient_name,
        "manager_brief": quote.manager_brief, "confirmed_terms": quote.commercial_terms,
        "selection_origin": "customer_request",
        "products": [{"product_id": f"variant:1:line:{line.line_number}", "name": line.name,
                      "quantity": str(line.sale_quantity), "unit": line.sale_unit, "brand": line.brand,
                      "requested_specs": [requirement.model_dump(mode="json", exclude={"source_quote"})
                                          for requirement in quote.source_request.lines[line.line_number - 1].requirements],
                      "verified_specs": _verified_specs(line)}
                     for line in quote.lines],
    }
    result = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(result) > 12_000:
        raise ValueError("presentation_context_too_large")
    return result


def render_pdf(quote: QuoteSnapshot) -> bytes:
    # Revalidate externally constructed/copy-updated snapshots before rendering.
    quote = QuoteSnapshot.model_validate_json(quote.model_dump_json())
    if not quote.ready_for_draft or quote.total_rub is None:
        raise ValueError("incomplete_draft")
    register_brand_fonts()
    accent = colors.HexColor("#237F83")
    body = ParagraphStyle("UniversalBody", fontName="KPDeskInter", fontSize=9,
                          leading=13, textColor=TEXT, spaceAfter=8)
    title = ParagraphStyle("UniversalTitle", parent=body, fontName="KPDeskInterBold",
                           fontSize=17, leading=22, textColor=INK, spaceAfter=12)
    small = ParagraphStyle("UniversalSmall", parent=body, fontSize=7, leading=10, textColor=MUTED)
    head = ParagraphStyle("UniversalHead", parent=small, fontName="KPDeskInterBold", textColor=INK)
    def paragraph(value: str, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(escape(value).replace("\n", "<br/>"), style)
    story: list[Flowable] = [proposal_header(
        quote.issuer_name, str(quote.quote_id)[:8] + "/" + str(quote.revision), "ЧЕРНОВИК",
        accent, compact=False, logo=kpdesk_default_logo()), Spacer(1, 14),
        paragraph("ЧЕРНОВИК · НЕ УТВЕРЖДЕНО", title),
        paragraph("Требует проверки менеджером. Документ не является утверждённым предложением.", small)]
    if quote.recipient_name:
        story.append(paragraph("Получатель: " + quote.recipient_name))
    if quote.narrative:
        story.extend([paragraph(quote.narrative.headline, title), paragraph(quote.narrative.opening),
                      paragraph(quote.narrative.solution)])
    rows = [[paragraph(text, head) for text in
             ("№", "Артикул / наименование", "Кол-во / ед.", "Цена / база, ₽", "Сумма, ₽")]]
    for line in quote.lines:
        if (line.price_rub is None or line.price_quantity is None or line.line_total_rub is None
            or line.sale_quantity is None):
            raise ValueError("incomplete_draft")
        specifications = "; ".join(f"{spec.name}: {spec.value}" + (" " + spec.unit if spec.unit else "")
                                   for spec in line.specifications)
        rows.append([paragraph(str(line.line_number), small),
                     paragraph((line.sku or "") + " · " + (line.name or "") +
                               ("\n" + specifications if specifications else "")),
                     paragraph(f"{line.sale_quantity} {line.sale_unit}"),
                     paragraph(f"{money(line.price_rub)} / {line.price_quantity} {line.sale_unit}", small),
                     paragraph(money(line.line_total_rub))])
    table = LongTable(rows, colWidths=[20, CONTENT_WIDTH - 225, 65, 80, 60],
                      repeatRows=1, hAlign="LEFT", splitInRow=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), SURFACE),
        ("LINEBELOW", (0, 0), (-1, 0), .6, BORDER),
        ("LINEBELOW", (0, 1), (-1, -1), .3, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([table, Spacer(1, 12), total_panel("Итого, ₽", quote.total_rub, accent, compact=False)])
    if quote.commercial_terms:
        story.extend([Spacer(1, 12), paragraph("Условия", head), paragraph(quote.commercial_terms)])
    if quote.narrative:
        story.append(paragraph(quote.narrative.closing))
    panels = party_panels(quote.issuer_details, quote.recipient_details, quote.issuer_contacts or "")
    if panels is not None:
        story.extend([Spacer(1, 12), panels])
    if quote.issuer_contacts and quote.issuer_details is None:
        story.extend([Spacer(1, 12), paragraph("Контакты отправителя", head),
                      paragraph(quote.issuer_contacts)])
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=48, rightMargin=48,
                                 topMargin=40, bottomMargin=40, title="KPDesk · Черновик")
    document.build(story)
    return buffer.getvalue()
