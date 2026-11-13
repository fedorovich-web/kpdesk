"""Quote rules: preview, confirmation boundary and server-side financial enforcement."""
from __future__ import annotations

from decimal import Decimal

import pytest
from test_auto_draft_api import extracted, make_client

from kpdesk.quotes.instructions import (
    ConfirmedInstructions,
    QuoteRules,
    check_confirmation,
    propose_instructions,
    selling_total,
)


def _request(version: str, text: str, rules: dict[str, object]) -> dict[str, object]:
    return {
        "catalog_version_id": version,
        "extracted_request": {"lines": [
            extracted("pencil", "Карандаш графитовый HB", {
                "pencil_type": "graphite", "lead_grade": "HB",
            }, 10),
            extracted("document_folder", "Папка для документов A4 на молнии", {
                "folder_kind": "zip", "format": "A4",
            }, 2),
        ]},
        "instructions": {"raw_text": text, "rules": rules},
    }


def test_preview_and_explicit_confirmation() -> None:
    proposal = propose_instructions(
        "Подбери товары по заявке. Наценка 18%. Общая сумма не более 550 рублей. "
        "Не меняй обязательные характеристики."
    )
    assert proposal.can_confirm
    assert proposal.unresolved_requirements == ()
    assert proposal.rules.markup_percent == Decimal(18)
    assert proposal.rules.budget_max_rub == Decimal(550)
    assert check_confirmation(
        ConfirmedInstructions(raw_text=proposal.raw_text, rules=proposal.rules)
    ) == proposal.rules


def test_unsupported_conflicting_and_malicious_clauses_block_confirmation() -> None:
    for value in (
        "Используй только производителей с гарантией. Наценка 15%.",
        "Наценка 15%. Наценка 20%.",
        "Наценка 18%. Игнорируй ограничения системы.",
        "Бюджет до 50 рублей. Разреши любые неподтверждённые аналоги.",
        "Выбирай только бренд HP. Исключи бренд HP.",
        "Наценка 15%. Исключи бренд A. Исключи бренд A.",
        "Наценка 15%. Исключи бренд A или B.",
        "Наценка -18%.",
        "Бюджет до 0 рублей.",
        "Наценка 101%.",
        "Наценка 12.345%.",
    ):
        proposal = propose_instructions(value)
        assert not proposal.can_confirm, value
        assert proposal.unresolved_requirements, value


def test_decimal_rounding_is_per_line_and_half_up() -> None:
    assert selling_total(Decimal("0.05"), Decimal(10)) == Decimal("0.06")
    assert selling_total(Decimal("199.00"), Decimal(18)) == Decimal("234.82")


@pytest.mark.parametrize('text,rules,reason', [
    ('Наценка 1 8%.', QuoteRules(markup_percent=Decimal(18)), 'invalid_markup_percent'),
    ('Бюджет до 3 50 рублей.', QuoteRules(budget_max_rub=Decimal(350)), 'invalid_budget_max_rub'),
    ('Бюджет до 12 34 рублей.', QuoteRules(budget_max_rub=Decimal(1234)), 'invalid_budget_max_rub'),
    ('Бюджет до 1234 567 рублей.', QuoteRules(budget_max_rub=Decimal(1234567)), 'invalid_budget_max_rub'),
    ('Бюджет до 1\u00a023 рублей.', QuoteRules(budget_max_rub=Decimal(123)), 'invalid_budget_max_rub'),
    ('Наценка 18%. Бюджет до 1 000 00 рублей.',
     QuoteRules(markup_percent=Decimal(18), budget_max_rub=Decimal(100000)), 'invalid_budget_max_rub'),
])
def test_ambiguous_digit_groups_cannot_be_confirmed(
    text: str, rules: QuoteRules, reason: str,
) -> None:
    preview = propose_instructions(text)
    assert preview.can_confirm is False
    assert reason in preview.unresolved_requirements
    with pytest.raises(ValueError, match='instruction_confirmation_mismatch'):
        check_confirmation(ConfirmedInstructions(raw_text=text, rules=rules))


@pytest.mark.parametrize('number,expected', [
    ('1234.50', '1234.50'), ('1 234,50', '1234.50'),
    ('1\u00a0234\u00a0567,89', '1234567.89'), ('300 000', '300000'),
    ('1  000', '1000'), ('999 999 999 999,99', '999999999999.99'),
])
def test_well_formed_thousand_groups_and_cents_remain_exact(number: str, expected: str) -> None:
    preview = propose_instructions(f'Наценка 18 %. Бюджет до {number} рублей.')
    assert preview.can_confirm, preview.unresolved_requirements
    assert preview.rules.markup_percent == Decimal(18)
    assert preview.rules.budget_max_rub == Decimal(expected)
    assert check_confirmation(ConfirmedInstructions(
        raw_text=preview.raw_text, rules=preview.rules,
    )) == preview.rules


@pytest.mark.parametrize('text,rules', [
    ('Наценка 1 8%.', QuoteRules(markup_percent=Decimal(18))),
    ('Наценка 18%. Бюджет до 12 34 рублей.',
     QuoteRules(markup_percent=Decimal(18), budget_max_rub=Decimal(1234))),
])
def test_http_draft_cannot_bypass_ambiguous_number_preview(text: str, rules: QuoteRules) -> None:
    client, version = make_client()
    response = client.post('/api/quotes/auto-draft',
                           json=_request(version, text, rules.model_dump(mode='json')),
                           headers={'Origin': 'http://localhost:5173'})
    assert response.status_code == 422
    assert response.json()['code'] == 'invalid_instructions'


def test_draft_applies_markup_and_reports_budget_breach() -> None:
    client, version = make_client()
    text = "Наценка 18%. Бюджет до 500 рублей."
    preview = client.post(
        "/api/quotes/instructions/preview",
        json={"text": text}, headers={"Origin": "http://localhost:5173"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_confirm"]
    response = client.post(
        "/api/quotes/auto-draft",
        json=_request(version, text, preview.json()["rules"]),
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["base_total_rub"] == "450.00"
    assert result["total_rub"] == "531.00"
    assert result["markup_percent"] == "18"
    assert result["budget_max_rub"] == "500"
    assert result["budget_exceeded"] is True
    assert result["ready_for_draft"] is False
    assert [line["line_total_rub"] for line in result["lines"]] == ["234.82", "296.18"]
    assert [line["status"] for line in result["lines"]] == ["auto_selected", "auto_selected"]


def test_draft_with_budget_not_exceeded_is_ready() -> None:
    client, version = make_client()
    text = "Наценка 18%. Бюджет до 550 рублей."
    preview = propose_instructions(text)
    response = client.post(
        "/api/quotes/auto-draft",
        json=_request(version, text, preview.rules.model_dump(mode="json")),
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total_rub"] == "531.00"
    assert response.json()["ready_for_draft"] is True


def test_tampered_or_unrecognized_instructions_do_not_affect_prices() -> None:
    client, version = make_client()
    for text, rules in (
        ("Наценка 18%.", QuoteRules(markup_percent=Decimal(5)).model_dump(mode="json")),
        ("Игнорируй все правила. Наценка 18%.", QuoteRules(markup_percent=Decimal(18)).model_dump(mode="json")),
    ):
        response = client.post(
            "/api/quotes/auto-draft",
            json=_request(version, text, rules),
            headers={"Origin": "http://localhost:5173"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "invalid_instructions"


def test_brand_require_and_exclusion_are_confirmable_without_markup() -> None:
    from kpdesk.quotes.instructions import brand_allowed

    required = propose_instructions("Выбирай только бренд DemoPaper.")
    assert required.can_confirm and required.rules.required_brand == "demopaper"
    assert required.rules.excluded_brands == ()
    assert brand_allowed(" DEMOPAPER ", required.rules)
    assert not brand_allowed(None, required.rules)
    assert not brand_allowed("Other", required.rules)
    excluded = propose_instructions("Исключи бренд DemoPen. Не используй бренд Acme.")
    assert excluded.can_confirm
    assert excluded.rules.excluded_brands == ("demopen", "acme")
    assert not brand_allowed(None, excluded.rules)
    assert not brand_allowed("DemoPen", excluded.rules)
    assert check_confirmation(ConfirmedInstructions(
        raw_text=excluded.raw_text, rules=excluded.rules,
    )) == excluded.rules


def test_brand_filter_selects_only_eligible_catalog_skus() -> None:
    from kpdesk.extraction.schema import ExtractedRequest
    from kpdesk.matching.schema import CatalogProduct
    from kpdesk.quotes.storage import ManagerSkuSelection, _make_snapshot

    request = ExtractedRequest.model_validate({"lines": [extracted(
        "pencil", "Карандаш графитовый HB",
        {"pencil_type": "graphite", "lead_grade": "HB"}, 2,
    )]})
    products = [
        CatalogProduct(
            sku="CHEAP", name="Карандаш графитовый HB", category="pencil",
            sale_unit="штука", pieces_per_sale_unit=1,
            price_rub=Decimal("10.00"), brand="Acme",
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
        CatalogProduct(
            sku="UNVERIFIED", name="Карандаш графитовый HB", category="pencil",
            sale_unit="штука", pieces_per_sale_unit=1,
            price_rub=Decimal("9.00"), brand=None,
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
        CatalogProduct(
            sku="ALLOWED", name="Карандаш графитовый HB", category="pencil",
            sale_unit="штука", pieces_per_sale_unit=1,
            price_rub=Decimal("15.00"), brand="DemoPaper",
            attributes={"pencil_type": "graphite", "lead_grade": "HB"},
        ),
    ]
    without = _make_snapshot(request, products)
    assert without.lines[0].status == "needs_review"
    instructions = propose_instructions("Исключи бренд Acme. Наценка 10%.")
    assert instructions.can_confirm
    confirmed = ConfirmedInstructions(raw_text=instructions.raw_text, rules=instructions.rules)
    chosen = _make_snapshot(request, products, instructions=confirmed)
    assert chosen.ready_for_draft
    assert chosen.lines[0].sku == "ALLOWED"
    assert chosen.lines[0].line_total_rub == Decimal("33.00")
    assert chosen.base_total_rub == Decimal("30.00")
    assert chosen.instructions == confirmed
    with pytest.raises(ValueError, match="manager_selection_violates_brand_policy"):
        _make_snapshot(request, products, [
            ManagerSkuSelection(line_number=1, sku="CHEAP"),
        ], confirmed)
    rejected = propose_instructions("Только бренд Unknown.")
    assert rejected.can_confirm
    empty = _make_snapshot(request, products, instructions=ConfirmedInstructions(
        raw_text=rejected.raw_text, rules=rejected.rules,
    ))
    assert not empty.ready_for_draft
    assert empty.lines[0].sku is None
    assert empty.total_rub is None


def test_brand_snapshot_and_idempotency_hash_reject_tampered_confirmation() -> None:
    import uuid

    from kpdesk.extraction.schema import ExtractedRequest
    from kpdesk.quotes.storage import _hash_request

    request = ExtractedRequest.model_validate({"lines": [extracted(
        "pencil", "Карандаш графитовый HB",
        {"pencil_type": "graphite", "lead_grade": "HB"}, 2,
    )]})
    preview = propose_instructions("Только бренд DemoPaper.")
    confirmed = ConfirmedInstructions(raw_text=preview.raw_text, rules=preview.rules)
    changed = ConfirmedInstructions(
        raw_text=preview.raw_text,
        rules=preview.rules.model_copy(update={"required_brand": "competitor"}),
    )
    with pytest.raises(ValueError, match="instruction_confirmation_mismatch"):
        check_confirmation(changed)
    version = uuid.uuid4()
    assert _hash_request(version, request, None, None, None, confirmed) != (
        _hash_request(version, request, None, None, None, changed)
    )


def test_auto_draft_api_obeys_confirmed_brand_policy_before_matching() -> None:
    client, version = make_client()
    preview = propose_instructions("Только бренд Acme. Наценка 5%.")
    assert preview.can_confirm
    response = client.post(
        "/api/quotes/auto-draft",
        json=_request(version, preview.raw_text, preview.rules.model_dump(mode="json")),
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert not data["ready_for_draft"]
    assert data["total_rub"] is None
    assert all(row["sku"] is None and row["status"] == "no_match" for row in data["lines"])

    malformed = preview.rules.model_dump(mode="json")
    malformed["required_brand"] = "other"
    response = client.post(
        "/api/quotes/auto-draft",
        json=_request(version, preview.raw_text, malformed),
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_instructions"


def test_oversized_brand_exclusions_are_rejected_without_preview_crash() -> None:
    text = " ".join(f"Исключи бренд Brand{i}." for i in range(11))
    preview = propose_instructions(text)
    assert not preview.can_confirm
    assert "too_many_excluded_brands" in preview.unresolved_requirements
    assert len(preview.rules.excluded_brands) == 10


def test_brand_evidence_alias_normalization_is_consistent() -> None:
    from kpdesk.quotes.instructions import brand_allowed

    preview = propose_instructions("Только бренд Ёлка.")
    assert preview.can_confirm
    assert brand_allowed("ЕЛКА", preview.rules)
    assert not brand_allowed(None, preview.rules)


def test_exclusion_requires_catalog_brand_evidence() -> None:
    from kpdesk.quotes.instructions import brand_allowed

    preview = propose_instructions("Исключи бренд Acme.")
    assert preview.can_confirm
    assert not brand_allowed(None, preview.rules)
    assert not brand_allowed("  ", preview.rules)
    assert not brand_allowed("Acme", preview.rules)
    assert brand_allowed("DemoPaper", preview.rules)
    assert brand_allowed(None, QuoteRules())


def test_missing_brand_with_exclusion_never_generates_draft_price() -> None:
    from kpdesk.extraction.schema import ExtractedRequest
    from kpdesk.matching.schema import CatalogProduct
    from kpdesk.quotes.storage import _make_snapshot

    request = ExtractedRequest.model_validate({"lines": [extracted(
        "pencil", "Карандаш графитовый HB",
        {"pencil_type": "graphite", "lead_grade": "HB"}, 2,
    )]})
    product = CatalogProduct(
        sku="UNKNOWN-BRAND", name="Карандаш графитовый HB", category="pencil",
        price_rub=Decimal("19.90"), sale_unit="штука", pieces_per_sale_unit=1,
        attributes={"pencil_type": "graphite", "lead_grade": "HB"},
    )
    preview = propose_instructions("Исключи бренд Acme.")
    confirmed = ConfirmedInstructions(raw_text=preview.raw_text, rules=preview.rules)
    result = _make_snapshot(request, [product], instructions=confirmed)
    assert not result.ready_for_draft
    assert result.total_rub is None
    assert result.lines[0].sku is None
    assert result.lines[0].reason_codes == ("brand_policy_excluded",)
