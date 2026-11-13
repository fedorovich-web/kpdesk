"""Version 2 PDF visuals. Only trusted local assets and escaped saved facts."""
from decimal import Decimal
from html import escape
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, Image, Paragraph, Table, TableStyle

from kpdesk.company.logo import CompanyLogo
from kpdesk.company.requisites import PartyDetails

FONT_DIRECTORY = Path(__file__).resolve().parents[1] / "assets/fonts"
INK = colors.HexColor("#112C3A")
TEXT = colors.HexColor("#263E4F")
MUTED = colors.HexColor("#687F8E")
BORDER = colors.HexColor("#E1EAF0")
SURFACE = colors.HexColor("#F4F7F9")
CONTENT_WIDTH = A4[0] - 96  # Same 48-point text edges as the ReportLab frame.


def register_brand_fonts() -> None:
    for name, filename in (("KPDeskInter", "Inter-Regular.ttf"), ("KPDeskInterBold", "Inter-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIRECTORY / filename)))
    pdfmetrics.registerFontFamily("KPDeskInter", normal="KPDeskInter", bold="KPDeskInterBold",
                                 italic="KPDeskInter", boldItalic="KPDeskInterBold")


def money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", "\u00a0").replace(".", ",")


def tint(accent: colors.Color) -> colors.Color:
    return colors.Color(.95 + .05 * accent.red, .95 + .05 * accent.green, .95 + .05 * accent.blue)


def proposal_header(issuer: str, number: str, issued_on: str, accent: colors.Color,
                    *, compact: bool, logo: CompanyLogo | None = None) -> Table:
    label = ParagraphStyle("BrandHeaderLabel", fontName="KPDeskInter", fontSize=7,
                           leading=10, textColor=MUTED, spaceAfter=5)
    company = ParagraphStyle("BrandCompany", fontName="KPDeskInterBold", fontSize=13 if compact else 16,
                             leading=18 if compact else 22, textColor=INK)
    meta = ParagraphStyle("BrandHeaderMeta", parent=label, alignment=TA_RIGHT, fontSize=8, leading=12)
    number_style = ParagraphStyle("BrandNumber", parent=company, alignment=TA_RIGHT, fontSize=11, leading=15)
    meta_label = "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ"
    meta_width = pdfmetrics.stringWidth(meta_label, meta.fontName, meta.fontSize) + 16
    sender_width = CONTENT_WIDTH - meta_width
    sender: list[Flowable] = [Paragraph("ОТПРАВИТЕЛЬ", label), Paragraph(escape(issuer), company)]
    if logo:
        ratio = logo.width / logo.height
        width = min(96., 40 * ratio)
        image = Image(BytesIO(logo.png_bytes()), width=width, height=width / ratio, hAlign="LEFT")
        logo_row = Table([[image, sender]], colWidths=[width + 10, sender_width - width - 10], hAlign="LEFT")
        logo_row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        sender = [logo_row]
    result = Table([[
        sender,
        [Paragraph(meta_label, meta),
         Paragraph("№ " + escape(number), number_style), Paragraph(escape(issued_on), meta)],
    ]], colWidths=[sender_width, meta_width], hAlign="LEFT")
    result.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LINEBELOW", (0, -1), (-1, -1), 1.6, accent),
    ]))
    return result


def total_panel(label: str, value: Decimal, accent: colors.Color, *, compact: bool) -> Table:
    title = ParagraphStyle("BrandTotalLabel", fontName="KPDeskInterBold", fontSize=10, leading=15, textColor=INK)
    amount = ParagraphStyle("BrandTotalAmount", fontName="KPDeskInterBold", fontSize=14 if compact else 18,
                            leading=20 if compact else 24, textColor=INK, alignment=TA_RIGHT)
    result = Table([[Paragraph(escape(label), title), Paragraph(money(value) + " ₽", amount)]],
                   colWidths=[270, CONTENT_WIDTH - 270], hAlign="LEFT")
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), tint(accent)),
        ("BOX", (0, 0), (-1, -1), .5, BORDER),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10 if compact else 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10 if compact else 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]))
    return result


def party_panels(issuer: PartyDetails | None, recipient: PartyDetails | None, contacts: str = "") -> Table | None:
    title = ParagraphStyle("BrandPartyTitle", fontName="KPDeskInterBold", fontSize=9, leading=13,
                           textColor=INK, spaceAfter=8)
    text_style = ParagraphStyle("BrandPartyText", fontName="KPDeskInter", fontSize=8, leading=12,
                                textColor=TEXT, wordWrap="CJK", spaceAfter=3)
    panels: list[list[Flowable]] = []
    for label, party in (("Реквизиты отправителя", issuer), ("Реквизиты заказчика", recipient)):
        if party is None:
            continue
        values = [party.full_name, "ИНН: " + party.inn]
        if party.kpp:
            values.append("КПП: " + party.kpp)
        if party.ogrn:
            values.append("ОГРН / ОГРНИП: " + party.ogrn)
        if party.address:
            values.append("Юридический адрес: " + party.address)
        panel: list[Flowable] = [Paragraph(label, title), *[Paragraph(escape(value), text_style) for value in values]]
        if party is issuer and contacts:
            panel.extend([Paragraph("Контакты", title), Paragraph(escape(contacts).replace("\n", "<br/>"), text_style)])
        panels.append(panel)
    if not panels:
        return None
    result = Table([[panels[0], "", panels[1]]] if len(panels) == 2 else [[panels[0]]],
                   colWidths=[(CONTENT_WIDTH - 12) / 2, 12, (CONTENT_WIDTH - 12) / 2] if len(panels) == 2 else [CONTENT_WIDTH],
                   hAlign="LEFT", splitInRow=1)
    style = TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ])
    for column in (0, 2) if len(panels) == 2 else (0,):
        style.add("BACKGROUND", (column, 0), (column, 0), SURFACE)
        style.add("BOX", (column, 0), (column, 0), .5, BORDER)
    result.setStyle(style)
    return result
