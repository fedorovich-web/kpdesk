import json
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from pypdf import PdfReader

from kpdesk.universal.document import narrative_facts, render_pdf
from kpdesk.universal.matching import build_quote
from kpdesk.universal.schema import (
    CatalogSnapshot,
    QuoteContext,
    RequestLine,
    Selection,
    UniversalProduct,
    UniversalRequest,
)


def test_pdf_brand_cyrillic_unapproved_and_facts_exclude_prices() -> None:
    c = CatalogSnapshot(version_id=uuid4(), source_sha256="a" * 64, products=(
        UniversalProduct(sku="P", name="Насос <безопасный>", sale_unit="шт", price_rub=Decimal("123.45")),))
    r = UniversalRequest(lines=(RequestLine(description="Насос", source_quote="Насос 2 шт", quantity=Decimal(2), sale_unit="шт"),))
    q = build_quote(c, r, (Selection(line_number=1, sku="P"),), QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    pdf = render_pdf(q)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "НЕ УТВЕРЖДЕНО" in text and "Насос <безопасный>" in text
    assert "246,90" in text
    facts = narrative_facts(q)
    assert "123.45" not in facts and "246.90" not in facts
    assert "Насос" in facts
    incomplete = build_quote(c, r, (), QuoteContext(issuer_name="Поставщик"), uuid4(), 1)
    with pytest.raises(ValueError):
        render_pdf(incomplete)


def test_narrative_facts_adapts_to_existing_writer_review_evidence_contract() -> None:
    from kpdesk.universal.schema import Specification
    c = CatalogSnapshot(version_id=uuid4(), source_sha256="a" * 64, products=(
        UniversalProduct(sku="M", name="Маркер", category="marker", brand="Example",
                         sale_unit="шт", price_rub=Decimal(10), specifications=(
                             Specification(name="marker_type", value="перманентный"),
                             Specification(name="цвет", value="синий"),
                             Specification(name="Цвет", value="красный"))),))
    r = UniversalRequest(lines=(RequestLine(description="Маркер", source_quote="Маркер 1 шт",
                                            quantity=Decimal(1), sale_unit="шт"),))
    q = build_quote(c, r, (Selection(line_number=1, sku="M"),),
                    QuoteContext(issuer_name="Компания", commercial_terms="Условия согласуются"), uuid4(), 1)
    payload = json.loads(narrative_facts(q))
    assert payload["issuer"] == "Компания"
    assert payload["confirmed_terms"] == "Условия согласуются"
    assert payload["selection_origin"] == "customer_request"
    product = payload["products"][0]
    assert product["product_id"] == "variant:1:line:1" and product["brand"] == "Example"
    assert product["verified_specs"]["marker_type"] == "перманентный"
    assert product["verified_specs"]["category"] == "marker"
    assert "цвет" not in product["verified_specs"] and "Цвет" not in product["verified_specs"]



@pytest.mark.asyncio
async def test_universal_multiproduct_narrative_isolates_facts_and_allows_one_bounded_correction(
    mock_llm_operation: object,
) -> None:
    import httpx

    from kpdesk.llm.settings import LLMSettings
    from kpdesk.quotes.document import ProposalNarrative
    from kpdesk.quotes.narrative import generate_proposal_narrative
    from kpdesk.universal.schema import Requirement, Specification

    del mock_llm_operation
    catalog = CatalogSnapshot(version_id=uuid4(), source_sha256="c" * 64, products=(
        UniversalProduct(sku="CAB-1", name="Кабель ВВГ", brand="Энерго", sale_unit="м",
                         price_rub=Decimal("100.00"), specifications=(
                             Specification(name="материал", value="медь"),)),
        UniversalProduct(sku="MRK-1", name="Перманентный черный маркер", category="marker",
                         sale_unit="шт", price_rub=Decimal("50.00"), specifications=(
                             Specification(name="marker_type", value="перманентный"),)),
    ))
    request = UniversalRequest(lines=(
        RequestLine(description="Кабель ВВГ", source_quote="Кабель ВВГ 10 м, материал медь",
                    quantity=Decimal(10), sale_unit="м", requirements=(
                        Requirement(name="материал", value="медь", source_quote="материал медь"),)),
        RequestLine(description="Маркер", source_quote="Маркер 2 шт",
                    quantity=Decimal(2), sale_unit="шт"),
    ))
    quote = build_quote(
        catalog, request,
        (Selection(line_number=1, sku="CAB-1"), Selection(line_number=2, sku="MRK-1")),
        QuoteContext(issuer_name="Поставщик", manager_brief="Офис на 12 рабочих мест."),
        uuid4(), 1,
    )
    facts = narrative_facts(quote)
    parsed = json.loads(facts)
    assert len(parsed["products"]) == 2
    assert parsed["products"][0]["verified_specs"] == {"материал": "медь"}
    assert parsed["products"][1]["verified_specs"] == {"category": "marker", "marker_type": "перманентный"}
    assert "100.00" not in facts and "50.00" not in facts and "1100.00" not in facts

    calls: list[dict[str, object]] = []
    first_draft = ProposalNarrative(
        headline="Предложение по кабелю и маркерам",
        opening="Подготовили состав для 12 рабочих мест.",
        solution="Кабель ВВГ с медной жилой и перманентный черный маркер для белой доски.",
        closing="Подтвердите состав предложения.",
    )
    fixed_solution = "Кабель ВВГ с медной жилой и перманентный черный маркер для маркировки."
    supported_review = {
        name: {"status": "supported", "issues": []} for name in ProposalNarrative.model_fields
    }

    def provider(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            payload: object = first_draft.model_dump()
        elif len(calls) == 2:
            revision_input = json.loads(body["messages"][-1]["content"])
            assert revision_input["editable_sections"] == ["solution"]
            assert revision_input["review"]["context_issues"][0]["reason"] == "unsupported_surface"
            payload = {"solution": fixed_solution}
        else:
            payload = supported_review
        return httpx.Response(200, json={
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        narrative = await generate_proposal_narrative(
            facts, LLMSettings(api_key="offline-dummy", data_mode="real_catalogs"), client,
        )
    assert narrative.solution == fixed_solution
    assert len(calls) == 3
