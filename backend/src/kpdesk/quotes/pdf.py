"""Deterministic draft PDF from a persisted, price-locked quote revision.

Cyrillic fonts are embedded from operator-controlled local paths. This module
never calls LLMs, re-reads current catalog prices, approves or transmits quotes.
"""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal
from html import escape
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

if TYPE_CHECKING:
    from reportlab.pdfgen.canvas import Canvas

from kpdesk.company.default_logo import kpdesk_default_logo
from kpdesk.quotes.appearance import DocumentAppearance, header_text_color
from kpdesk.quotes.document import CommercialDocument
from kpdesk.quotes.instructions import brand_allowed, check_confirmation, selling_total
from kpdesk.quotes.line import LockedQuoteLine
from kpdesk.quotes.pdf_brand import (
    BORDER as BRAND_BORDER,
)
from kpdesk.quotes.pdf_brand import (
    CONTENT_WIDTH,
    FONT_DIRECTORY,
    money,
    party_panels,
    proposal_header,
    register_brand_fonts,
    total_panel,
)
from kpdesk.quotes.pdf_brand import (
    INK as BRAND_INK,
)
from kpdesk.quotes.pdf_brand import (
    MUTED as BRAND_MUTED,
)
from kpdesk.quotes.pdf_brand import (
    TEXT as BRAND_TEXT,
)
from kpdesk.quotes.storage import StoredQuote

# Only operator-controlled environment settings can select a PDF font.
# Keep defaults OS-native; production images must provision fonts with Cyrillic glyphs.
_WINDOWS_FONTS = os.name == "nt"
_FONT = Path(os.environ.get(
    "KP_DESK_PDF_FONT_REGULAR",
    "C:/Windows/Fonts/arial.ttf" if _WINDOWS_FONTS
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
))
_FONT_BOLD = Path(os.environ.get(
    "KP_DESK_PDF_FONT_BOLD",
    "C:/Windows/Fonts/arialbd.ttf" if _WINDOWS_FONTS
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
))
_DARK = colors.HexColor("#243B53")
_GRAY = colors.HexColor("#526477")
_BORDER = colors.HexColor("#D9E2EC")
_ACCENT = colors.HexColor("#F0F4F8")


class QuotePDFError(ValueError):
    pass


def _fonts(font_path: Path | None) -> None:
    if font_path is not None and font_path.resolve() != _FONT.resolve():
        raise QuotePDFError("font_not_allowed")
    if not _FONT.is_file() or not _FONT_BOLD.is_file():
        raise QuotePDFError("cyrillic_font_missing")
    if "KPDeskArial" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("KPDeskArial", str(_FONT)))
    if "KPDeskArialBold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("KPDeskArialBold", str(_FONT_BOLD)))


def _validate_quote(quote: StoredQuote) -> None:
    if (not quote.ready_for_draft or quote.total_rub is None
        or not 1 <= len(quote.lines) <= 20):
        raise QuotePDFError("incomplete_draft")
    if quote.approved and (
        quote.document is None or quote.approval is None
        or quote.approval.source_revision != quote.revision - 1
        or quote.approval.approved_at.tzinfo is None
    ):
        raise QuotePDFError("incomplete_commercial_document")
    if any(
        line.status not in ("auto_selected", "manager_selected") or line.sku is None
        or line.unit_price_rub is None or line.line_total_rub is None
        or line.sale_quantity is None or line.sale_unit is None
        or line.sale_quantity <= 0
        for line in quote.lines
    ):
        raise QuotePDFError("incomplete_draft")
    computed = sum(
        (line.line_total_rub for line in quote.lines if line.line_total_rub is not None),
        Decimal("0.00"),
    )
    if computed != quote.total_rub:
        raise QuotePDFError("inconsistent_quote_total")
    try:
        markup = (
            check_confirmation(quote.instructions).markup_percent
            if quote.instructions is not None else Decimal(0)
        )
    except ValueError as error:
        raise QuotePDFError("inconsistent_quote_rules") from error
    computed_base = Decimal("0.00")
    for line in quote.lines:
        if line.unit_price_rub is None or line.sale_quantity is None:
            raise QuotePDFError("incomplete_draft")
        base = line.unit_price_rub * line.sale_quantity
        computed_base += base
        if line.line_total_rub != selling_total(base, markup):
            raise QuotePDFError("inconsistent_quote_total")
    if quote.base_total_rub is not None and quote.base_total_rub != computed_base:
        raise QuotePDFError("inconsistent_quote_total")
    if quote.instructions is not None:
        if any(not brand_allowed(line.product_brand, quote.instructions.rules)
               for line in quote.lines):
            raise QuotePDFError("inconsistent_quote_brand_policy")
        budget = quote.instructions.rules.budget_max_rub
        if quote.budget_exceeded or (budget is not None and quote.total_rub > budget):
            raise QuotePDFError("incomplete_draft")
    if quote.approved and quote.document is not None:
        _validate_alternatives(quote, quote.document)


def _validate_alternatives(quote: StoredQuote, document: CommercialDocument) -> None:
    seen = {tuple((line.sku, line.sale_quantity) for line in quote.lines)}
    for alternative in document.alternatives:
        identity = tuple((line.sku, line.sale_quantity) for line in alternative.lines)
        if (identity in seen or alternative.catalog_version_id != quote.catalog_version_id
            or [line.line_number for line in alternative.lines] != list(range(1, len(quote.lines) + 1))
            or [(line.line_number, line.sku) for line in alternative.lines]
            != [(selection.line_number, selection.sku) for selection in alternative.selection.selections]):
            raise QuotePDFError("inconsistent_document_alternative")
        seen.add(identity)
        _validate_quote(replace(
            quote, lines=alternative.lines, total_rub=alternative.total_rub,
            base_total_rub=None, estimated_total_rub=None, budget_exceeded=False,
            approved=False, document=None, approval=None,
        ))



def _composition_table(
    lines: tuple[LockedQuoteLine, ...], commercial: bool,
    heading_style: ParagraphStyle, cell_style: ParagraphStyle, right_style: ParagraphStyle,
    appearance: DocumentAppearance | None = None,
) -> LongTable:
    row_headers = (
        ["№", "Артикул", "Наименование", "Кол-во", "Ед.", "Стоимость, ₽"]
        if commercial else
        ["№", "Артикул", "Наименование", "Кол-во", "Ед.", "Закупка, ₽", "Сумма, ₽"]
    )
    table_data: list[list[object]] = [
        [Paragraph(escape(x), heading_style) for x in row_headers],
    ]
    branded = appearance is not None and appearance.layout_version == 2
    for line in lines:
        if (line.sale_quantity is None or line.sale_unit is None
            or line.unit_price_rub is None or line.line_total_rub is None):
            raise QuotePDFError("incomplete_draft")
        text = escape(line.product_name or line.description)
        cells = [
            str(line.line_number),
            escape(line.sku or ""),
            text,
            str(line.sale_quantity),
            escape(line.sale_unit),
            f"{line.unit_price_rub:.2f}",
            money(line.line_total_rub) if branded else f"{line.line_total_rub:.2f}",
        ]
        if commercial:
            del cells[5]  # Procurement cost is internal; client sees selling line totals.
        table_data.append([
            Paragraph(item, right_style if i in (0, 3, 5, 6) else cell_style)
            for i, item in enumerate(cells)
        ])
    table = LongTable(
        table_data,
        colWidths=([20, 95, CONTENT_WIDTH - 296, 40, 45, 96] if branded else [21, 65, 240, 45, 55, 85]) if commercial else [21, 70, 169, 42, 48, 80, 81],
        repeatRows=1, hAlign="LEFT",
    )
    accent = colors.HexColor(appearance.accent_color) if appearance else _DARK
    tint = colors.Color(.95 + .05 * accent.red, .95 + .05 * accent.green, .95 + .05 * accent.blue) if appearance else _ACCENT
    compact = appearance is not None and appearance.template == "compact"
    presentation = appearance is not None and appearance.template == "presentation"
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, tint]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6 if compact else 7 if presentation else 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6 if compact or presentation else 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, _BORDER),
    ]))
    return table


def _render_pdf(quote: StoredQuote, *, font_path: Path | None = None, preview_document: CommercialDocument | None = None) -> bytes:
    """Render the saved revision: internal draft or explicitly approved offer.

    The historical function name remains for existing internal-preview callers.
    No text or prices are generated at download time.
    """
    _validate_quote(quote)
    buffer = BytesIO()
    commercial = preview_document or (quote.document if quote.approved else None)
    if preview_document:
        _validate_alternatives(quote, preview_document)
    appearance = commercial.appearance if commercial else None
    branded = appearance is not None and appearance.layout_version == 2
    if branded:
        if font_path is not None and font_path.resolve() != _FONT.resolve():
            raise QuotePDFError("font_not_allowed")
        if not all((FONT_DIRECTORY / name).is_file() for name in ("Inter-Regular.ttf", "Inter-Bold.ttf")):
            raise QuotePDFError("brand_font_missing")
        register_brand_fonts()
    else:
        _fonts(font_path)
    compact = appearance is not None and appearance.template == "compact"
    presentation = appearance is not None and appearance.template == "presentation"

    def space(points: float) -> Spacer:
        return Spacer(1, points * .5 if compact else points)

    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=42, rightMargin=42,
        topMargin=54 if branded else 42 if appearance else 62, bottomMargin=58,
        title="Коммерческое предложение" if commercial else "KPDesk - Черновик коммерческого предложения",
        author=commercial.issuer_name if commercial else "KPDesk",
        subject="Предпросмотр — не утверждено" if preview_document else (
            "Коммерческое предложение" if commercial else "Не утверждено и не отправлено"
        ),
        invariant=1,
    )
    title_style = ParagraphStyle(
        "KPQuoteTitle", fontName="KPDeskArialBold", fontSize=16, leading=21,
        textColor=_DARK, alignment=TA_LEFT, spaceAfter=10,
    )
    small_style = ParagraphStyle(
        "KPQuoteMeta", fontName="KPDeskArial", fontSize=8.5, leading=13,
        textColor=_GRAY, alignment=TA_LEFT,
    )
    cell_style = ParagraphStyle(
        "KPQuoteCell", fontName="KPDeskArial", fontSize=8, leading=11.5,
        textColor=_DARK, alignment=TA_LEFT, wordWrap="CJK",
    )
    right_style = ParagraphStyle(
        "KPQuoteRight", parent=cell_style, alignment=TA_RIGHT,
    )
    heading_style = ParagraphStyle(
        "KPQuoteHeading", fontName="KPDeskArialBold", fontSize=8.3,
        leading=12, textColor=colors.white, alignment=TA_LEFT,
    )
    note_style = ParagraphStyle(
        "KPQuoteNote", parent=small_style, textColor=_DARK,
    )
    section_style = ParagraphStyle(
        "KPQuoteSection", parent=note_style, keepWithNext=True, spaceAfter=6,
    )
    prose_style = ParagraphStyle(
        "KPQuoteProse", parent=note_style, fontSize=10, leading=16, spaceAfter=10,
    )
    if appearance:
        title_style.fontSize = 16 if compact else 22
        title_style.leading = 21 if compact else 28
        title_style.wordWrap = "CJK"
        prose_style.fontSize = 9.5 if compact else 10.5
        prose_style.leading = 14 if compact else 17
        prose_style.spaceAfter = 8
        prose_style.wordWrap = "CJK"
        heading_style.textColor = colors.HexColor(header_text_color(appearance.accent_color))
        section_style.fontName = "KPDeskArialBold"
        section_style.fontSize = 10
    if branded:
        for style in (title_style, small_style, cell_style, right_style, heading_style,
                      note_style, section_style, prose_style):
            style.fontName = "KPDeskInterBold" if style in (title_style, heading_style, section_style) else "KPDeskInter"
        title_style.textColor = section_style.textColor = BRAND_INK
        prose_style.textColor = cell_style.textColor = right_style.textColor = note_style.textColor = BRAND_TEXT
        small_style.textColor = BRAND_MUTED
        section_style.spaceBefore = 4
        section_style.spaceAfter = 4
        prose_style.spaceAfter = 6
        prose_style.fontSize = 9 if compact else 10
        prose_style.leading = 13 if compact else 15
        title_style.fontSize = 17 if compact else 21
        title_style.leading = 22 if compact else 27

    story: list[Flowable] = [
        Paragraph("Коммерческое предложение — Черновик", title_style),
        Paragraph("Не утверждено. Не отправлено клиенту.", small_style),
        space(11),
        Paragraph(f"Документ: {escape(str(quote.quote_id))}", small_style),
        Paragraph(f"Ревизия: {quote.revision}", small_style),
        Paragraph(
            ("Основной каталог: " if any(line.supplier_id for line in quote.lines)
             else "Каталог: ") + escape(str(quote.catalog_version_id)), small_style,
        ),
        space(19),
    ]
    if commercial is not None:
        story = [
            Paragraph(escape(commercial.issuer_name), title_style),
            Paragraph(
                f"Коммерческое предложение № {quote.quote_id.hex[:8].upper()}/{quote.revision}",
                note_style,
            ),
            Paragraph(f"Дата: {commercial.issued_on:%d.%m.%Y}", small_style),
        ]
        if commercial.recipient_name:
            story.append(Paragraph("Для: " + escape(commercial.recipient_name), note_style))
        if appearance:
            issuer_style = ParagraphStyle("KPCompanyName", parent=title_style,
                                          fontSize=12 if compact else 16,
                                          leading=17 if compact else 22)
            metadata_style = ParagraphStyle("KPDocumentMeta", parent=note_style, alignment=TA_RIGHT)
            header = Table([[
                Paragraph(escape(commercial.issuer_name), issuer_style),
                Paragraph(f"Коммерческое предложение<br/>№ {quote.quote_id.hex[:8].upper()}/{quote.revision}"
                          f"<br/>{commercial.issued_on:%d.%m.%Y}", metadata_style),
            ]], colWidths=[340, 171], hAlign="LEFT")
            header.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("LINEBELOW", (0, -1), (-1, -1), .8, colors.HexColor(appearance.accent_color)),
            ]))
            story = [header]
            if branded:
                logo = commercial.company_profile.data.logo if commercial.company_profile else None
                if logo is None and appearance.default_logo == "kpdesk_v1":
                    try:
                        logo = kpdesk_default_logo()
                    except (OSError, ValueError) as error:
                        raise QuotePDFError("default_logo_unavailable") from error
                story = [proposal_header(commercial.issuer_name,
                         f"{quote.quote_id.hex[:8].upper()}/{quote.revision}",
                         f"{commercial.issued_on:%d.%m.%Y}", colors.HexColor(appearance.accent_color), compact=compact,
                         logo=logo)]
            if commercial.recipient_name:
                story.extend([space(10), Paragraph("Для: " + escape(commercial.recipient_name), note_style)])
        story.extend([
            Spacer(1, 10 if branded else 12 if compact else 16 if presentation else 20), Paragraph(escape(commercial.narrative.headline), title_style),
            Paragraph(escape(commercial.narrative.opening).replace("\n", "<br/>"), prose_style),
        ])
        if appearance:
            story.append(Paragraph("Предлагаемое решение", section_style))
        story.extend([
            Paragraph(escape(commercial.narrative.solution).replace("\n", "<br/>"), prose_style),
            space(0 if presentation else 12),
        ])
    if quote.instructions is not None and commercial is None:
        brand_rules = quote.instructions.rules
        brand_notes = (
            ("; только бренд: " + escape(brand_rules.required_brand))
            if brand_rules.required_brand else ""
        )
        if brand_rules.excluded_brands:
            brand_notes += "; исключены: " + escape(", ".join(brand_rules.excluded_brands))
        story.extend([
            Paragraph(
                f"Наценка: {brand_rules.markup_percent}%"
                + (
                    f"; бюджет: {quote.instructions.rules.budget_max_rub:.2f} ₽"
                    if brand_rules.budget_max_rub is not None else ""
                ) + brand_notes, small_style,
            ),
            space(8),
        ])
    if commercial is not None:
        composition_style = section_style
        if appearance:
            # keepWithNext glues the heading to the *entire* LongTable and
            # defers it to a fresh page. Reserve only a header and first row;
            # the remaining rows must be free to split in the current frame.
            composition_style = ParagraphStyle(
                "KPCompositionSection", parent=section_style, keepWithNext=False,
            )
        composition_heading = Paragraph(
            "Вариант 1 — основной состав" if commercial.alternatives else "Состав предложения",
            composition_style,
        )
        if appearance:
            first_row = _composition_table(
                quote.lines[:1], True, heading_style, cell_style, right_style, appearance,
            )
            _, table_height = first_row.wrap(doc.width, doc.height)
            _, heading_height = composition_heading.wrap(doc.width, doc.height)
            story.append(CondPageBreak(
                heading_height + composition_heading.getSpaceAfter() + table_height,
            ))
        story.append(composition_heading)
    story.extend([_composition_table(
        quote.lines, commercial is not None, heading_style, cell_style, right_style, appearance,
    ), space(12)])
    sourced_lines = [line for line in quote.lines if line.supplier_id is not None]
    if sourced_lines and commercial is None:
        story.append(Paragraph(
            "Источники цен выбранных строк (не подтверждают наличие товара):",
            small_style,
        ))
        for line in sourced_lines:
            story.append(Paragraph(
                f"Строка {line.line_number}: поставщик {escape(line.supplier_id or '')}; "
                f"версия {escape(str(line.source_catalog_version_id))}; "
                f"SHA-256 {escape(line.source_sha256 or '')}",
                small_style,
            ))
    # Presentation paragraphs already carry their own trailing space. Avoid
    # stacking it with extra spacers, especially around the table and total.
    total_style = ParagraphStyle(
        "KPQuoteTotal", parent=title_style, fontSize=13, alignment=TA_RIGHT,
    )
    if presentation:
        total_style.leading = 18
        total_style.spaceAfter = 0
    total_label = 'Итого по варианту 1' if commercial and commercial.alternatives else 'Итого'
    if branded and appearance:
        assert quote.total_rub is not None  # checked before rendering
        story.extend([total_panel(total_label, quote.total_rub, colors.HexColor(appearance.accent_color), compact=compact), space(12)])
    else:
        story.extend([
            space(0 if presentation else 18),
            Paragraph(f"<b>{total_label}: {quote.total_rub:.2f} ₽</b>", total_style),
            space(12),
        ])
    if commercial is not None:
        if commercial.alternatives:
            story.append(Paragraph("Варианты приведены на выбор. Их суммы не складываются.", prose_style))
            for number, alternative in enumerate(commercial.alternatives, 2):
                assert quote.total_rub is not None  # validated before rendering
                difference = alternative.total_rub - quote.total_rub
                changed = sum(line.sku != primary.sku or line.sale_quantity != primary.sale_quantity
                              for primary, line in zip(quote.lines, alternative.lines, strict=True))
                comparison = "Стоимость совпадает с основным вариантом." if difference == 0 else (
                    f"Стоимость {'меньше' if difference < 0 else 'больше'} основного варианта "
                    f"на {money(abs(difference)) if branded else format(abs(difference), '.2f')} ₽."
                )
                story.extend([
                    space(18), Paragraph(f"Вариант {number}", title_style),
                    Paragraph(f"Изменено позиций: {changed}. {comparison}", prose_style),
                    Paragraph("Что меняется", section_style),
                    Paragraph(f"Без замены: {len(quote.lines) - changed} из {len(quote.lines)} позиций.", prose_style),
                ])
                for primary, line in zip(quote.lines, alternative.lines, strict=True):
                    if primary.sku == line.sku and primary.sale_quantity == line.sale_quantity:
                        continue
                    quantity = (
                        f"Количество и единица продажи сохранены: {line.sale_quantity} {line.sale_unit}."
                        if (primary.sale_quantity, primary.sale_unit) == (line.sale_quantity, line.sale_unit)
                        else f"Количество: {primary.sale_quantity} {primary.sale_unit} → {line.sale_quantity} {line.sale_unit}."
                    )
                    story.append(Paragraph(
                        f"Позиция {line.line_number}: {escape(primary.product_name or '')} → "
                        f"{escape(line.product_name or '')}. {escape(quantity)}", prose_style,
                    ))
                story.extend([
                    _composition_table(alternative.lines, True, heading_style, cell_style, right_style, appearance),
                    space(12), total_panel(f"Итого по варианту {number}", alternative.total_rub,
                                          colors.HexColor(appearance.accent_color), compact=compact) if branded and appearance else Paragraph(f"<b>Итого по варианту {number}: {alternative.total_rub:.2f} ₽</b>",
                                           ParagraphStyle(f"KPAlternativeTotal{number}", parent=total_style)),
                    space(12),
                ])
            story.extend([Paragraph("Следующий шаг", section_style),
                          Paragraph("Сообщите выбранный вариант, чтобы согласовать состав заказа, оплату и поставку.", prose_style),
                          space(12)])
        if appearance and appearance.show_company_section and commercial.company_profile:
            company = commercial.company_profile.data
            if company.about or company.advantages:
                story.extend([space(0 if presentation else 12), Paragraph("О компании", section_style)])
            if company.about:
                story.append(Paragraph(escape(company.about).replace("\n", "<br/>"), prose_style))
            if company.advantages:
                story.extend([Paragraph("Преимущества компании", section_style),
                              Paragraph(escape(company.advantages).replace("\n", "<br/>"), prose_style)])
        story.extend([
            Paragraph("Условия предложения", section_style),
            Paragraph(escape(commercial.commercial_terms or
                "Условия оплаты и поставки подлежат отдельному согласованию."
            ).replace("\n", "<br/>"), prose_style),
            space(0 if presentation else 10),
            Paragraph(escape(commercial.narrative.closing).replace("\n", "<br/>"), prose_style),
        ])
        if (commercial.company_profile and commercial.company_profile.data.contacts
            and not (branded and commercial.company_profile.data.issuer_details)):
            story.extend([
                space(0 if presentation else 10), Paragraph("Контакты", section_style),
                Paragraph(escape(commercial.company_profile.data.contacts).replace("\n", "<br/>"), prose_style),
            ])
        issuer_details = commercial.company_profile.data.issuer_details if commercial.company_profile else None
        if branded:
            panels = party_panels(issuer_details, commercial.recipient_details,
                                  commercial.company_profile.data.contacts if commercial.company_profile else "")
            if panels:
                story.extend([space(12), panels])
        for label, party in (("Реквизиты отправителя", issuer_details),
                             ("Реквизиты заказчика", commercial.recipient_details)):
            if not party or branded:
                continue
            details = [party.full_name, "ИНН: " + party.inn]
            if party.kpp:
                details.append("КПП: " + party.kpp)
            if party.ogrn:
                details.append("ОГРН / ОГРНИП: " + party.ogrn)
            if party.address:
                details.append("Юридический адрес: " + party.address)
            story.extend([space(10), Paragraph(label, section_style),
                          Paragraph("<br/>".join(escape(value) for value in details), prose_style)])
    else:
        story.append(Paragraph(
            "Суммы рассчитаны по зафиксированной версии товарного прайса. "
            "Колонка «Закупка» содержит исходную цену единицы продажи; "
            "сумма строки включает подтверждённую наценку, если она задана. "
            "НДС, скидки, доставка и остатки не определены и не включены. "
            "Документ предназначен для внутренней проверки, "
            "не является утверждённым предложением.", note_style,
        ))

    def on_page(canvas: Canvas, document: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setFont("KPDeskInter" if branded else "KPDeskArial", 8)
        canvas.setFillColor(BRAND_MUTED if branded else _GRAY)
        edge = 48 if branded else 42
        if branded and commercial and document.page > 1:
            canvas.drawString(edge, A4[1] - 33,
                              f"Коммерческое предложение № {quote.quote_id.hex[:8].upper()}/{quote.revision}")
            canvas.setStrokeColor(BRAND_BORDER)
            canvas.line(edge, A4[1] - 40, A4[0] - edge, A4[1] - 40)
        if appearance:
            canvas.setStrokeColor(colors.HexColor(appearance.accent_color))
            canvas.setLineWidth(.8)
            canvas.line(edge, 47, A4[0] - edge, 47)
        canvas.drawString(edge, 32, "ПРЕДПРОСМОТР · НЕ УТВЕРЖДЕНО" if preview_document else ("Коммерческое предложение" if commercial else "KPDesk · Черновик, не утверждено"))
        if preview_document:
            canvas.drawString(edge, A4[1] - 20, "ПРЕДПРОСМОТР · НЕ УТВЕРЖДЕНО")
        canvas.drawRightString(A4[0] - edge, 32, f"Страница {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    raw = buffer.getvalue()
    if not raw.startswith(b"%PDF-") or len(raw) > 5 * 1024 * 1024:
        raise QuotePDFError("invalid_generated_pdf")
    return raw


def render_draft_pdf(quote: StoredQuote, *, font_path: Path | None = None) -> bytes:
    """Render only the persisted internal draft or approved client document."""
    return _render_pdf(quote, font_path=font_path)


def render_document_preview(quote: StoredQuote, document: CommercialDocument) -> bytes:
    """Private, visibly marked render before approval, without creating a revision."""
    if quote.approved:
        raise QuotePDFError("quote_already_approved")
    return _render_pdf(quote, preview_document=document)
