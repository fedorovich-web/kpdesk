import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from kpdesk.llm.client import LLMError
from kpdesk.llm.settings import LLMSettings
from kpdesk.universal.extraction import extract_request
from kpdesk.universal.schema import CatalogSnapshot, UniversalProduct, UniversalRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("ungrounded", [False, True])
async def test_dynamic_extraction_uses_shared_client_and_grounded_source(ungrounded: bool) -> None:
    c = CatalogSnapshot(version_id=uuid4(), source_sha256="a" * 64, products=(
        UniversalProduct(sku="P", name="Насос", sale_unit="шт", price_rub=Decimal(100)),))
    source = "Насос 2 шт, напор не менее 50 м"
    payload = {"lines": [{"description": "Насос", "source_quote": source, "quantity": "2",
                              "sale_unit": "шт", "sku": None, "brand": None, "model": None,
                              "requirements": [{"name": "напор", "value": 50, "unit": "м", "operator": "min",
                                                 "source_quote": "напор не менее 90 м" if ungrounded else "напор не менее 50 м"}]}]}
    async def inference(*args: object, **kwargs: object) -> object:
        return kwargs["parse"](json.dumps(payload))  # type: ignore[operator]
    with patch("kpdesk.universal.extraction.structured_inference", new=AsyncMock(side_effect=inference)) as mocked:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500))) as client:
            if ungrounded:
                with pytest.raises(LLMError):
                    await extract_request(source, c, LLMSettings(api_key="offline-dummy"), client)
            else:
                result = await extract_request(source, c, LLMSettings(api_key="offline-dummy"), client)
                assert result.lines[0].requirements[0].name == "напор"
        assert mocked.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,value,quantity", [
    ("Насос -2 шт, цвет синий", "синий", "2"),
    ("Насос около 2 шт, цвет синий", "синий", "2"),
    ("Насос 2-3 шт, цвет синий", "синий", "3"),
    ("Насос 2 шт, цвет не синий", "синий", "2"),
])
async def test_grounding_rejects_negative_approximate_and_negated_facts(source: str, value: str, quantity: str) -> None:
    c = CatalogSnapshot(version_id=uuid4(), source_sha256="a" * 64, products=(
        UniversalProduct(sku="P", name="Насос", sale_unit="шт", price_rub=Decimal(100)),))
    payload = {"lines": [{"description": "Насос", "source_quote": source, "quantity": quantity,
                           "sale_unit": "шт", "requirements": [{"name": "цвет", "value": value,
                           "source_quote": source.split(", ")[1]}]}]}
    async def inference(*args: object, **kwargs: object) -> object:
        return kwargs["parse"](json.dumps(payload))  # type: ignore[operator]
    with patch("kpdesk.universal.extraction.structured_inference", new=AsyncMock(side_effect=inference)):
        async with httpx.AsyncClient() as client:
            with pytest.raises(LLMError):
                await extract_request(source, c, LLMSettings(api_key="offline-dummy"), client)


@pytest.mark.parametrize("source,description,quantity,sale_unit,requirement", [
    ("Кабель 2 м, цвет синий; Насос 1 шт, цвет красный", "Кабель", "2", "м",
     {"name": "цвет", "value": "красный", "operator": "eq"}),
    ("Насос 1 шт, напор не менее 50 м, длина не более 5 м", "Насос", "1", "шт",
     {"name": "напор", "value": 5, "unit": "м", "operator": "max"}),
    ("Насос 1 шт, напор не менее 50 м длина не более 5 м", "Насос", "1", "шт",
     {"name": "напор", "value": 5, "unit": "м", "operator": "max"}),
    ("Насос 1 шт, напор не более 50 м", "Насос", "1", "шт",
     {"name": "напор", "value": 50, "unit": "м", "operator": "eq"}),
    ("Кабель 1 шт, длина 2 м", "Кабель", "2", "м", None),
    ("Кабель 1 шт длина 2 м", "Кабель", "2", "м", None),
    ("Кабель 1 шт длина 1 м", "Кабель", "1", "м", None),
    ("Кабель 2 м, Насос 1 шт, цвет красный", "Кабель", "2", "м",
     {"name": "цвет", "value": "красный", "operator": "eq", "source_quote": "цвет красный"}),
    ("Насос 1 шт, напор не более 50 м", "Насос", "1", "шт",
     {"name": "напор", "value": 50, "unit": "м", "operator": "eq", "source_quote": "напор не более 50 м"}),
    ("Насос 1 шт, напор не более 50 м", "Насос", "1", "шт",
     {"name": "напор", "value": 50, "unit": "м", "operator": "min", "source_quote": "напор не более 50 м"}),
])
def test_grounding_binds_position_property_operator_and_purchase_quantity(
    source: str, description: str, quantity: str, sale_unit: str,
    requirement: dict[str, object] | None,
) -> None:
    from kpdesk.universal.extraction import _ground
    requirements = [{"source_quote": source, **requirement}] if requirement is not None else []
    request = UniversalRequest.model_validate({"lines": [{"description": description,
        "source_quote": source, "quantity": quantity, "sale_unit": sale_unit,
        "requirements": requirements}]})
    with pytest.raises(LLMError, match="invalid_output"):
        _ground(source, request)


def test_grounding_preserves_two_items_exact_thresholds_and_fractional_decimal() -> None:
    from kpdesk.universal.extraction import _ground
    source = "Кабель 2,5 м, цвет синий; Насос 1 шт, напор не менее 50 м, длина не более 5 м"
    request = UniversalRequest.model_validate({"lines": [
        {"description": "Кабель", "source_quote": "Кабель 2,5 м, цвет синий", "quantity": "2.5", "sale_unit": "м",
         "requirements": [{"name": "цвет", "value": "синий", "source_quote": "цвет синий"}]},
        {"description": "Насос", "source_quote": "Насос 1 шт, напор не менее 50 м, длина не более 5 м",
         "quantity": "1", "sale_unit": "шт", "requirements": [
             {"name": "напор", "value": 50, "unit": "м", "operator": "min", "source_quote": "напор не менее 50 м"},
             {"name": "длина", "value": 5, "unit": "м", "operator": "max", "source_quote": "длина не более 5 м"}]}]})
    _ground(source, request)


@pytest.mark.parametrize("source,description,source_quote,requirement", [
    ("Насос 1 шт, марка АБ-12", "Насос", None,
     {"name": "марка", "value": "АБ-1", "source_quote": "марка АБ-1"}),
    ("Насос 1 шт, точность 12.5", "Насос", None,
     {"name": "точность", "value": 12, "source_quote": "точность 12"}),
    ("Насос 1 шт, напор 50 м", "Насос", None,
     {"name": "напор", "value": 50, "source_quote": "напор 50"}),
    ("Не нужен Насос 1 шт", "Насос", "Насос 1 шт", None),
    ("Не нужен Насос 1 шт", "Не нужен Насос", None, None),
    ("Кабель 2 м. Насос 1 шт, цвет красный", "Кабель", None,
     {"name": "цвет", "value": "красный", "source_quote": "цвет красный"}),
    ("Насос 1 шт Кабель 2 м, цвет красный", "Насос", None,
     {"name": "цвет", "value": "красный", "source_quote": "цвет красный"}),
])
def test_grounding_rejects_truncation_negation_and_residual_purchase_identity(
    source: str, description: str, source_quote: str | None, requirement: dict[str, object] | None,
) -> None:
    from kpdesk.universal.extraction import _ground
    request = UniversalRequest.model_validate({"lines": [{"description": description,
        "source_quote": source_quote or source, "quantity": "2" if description == "Кабель" else "1",
        "sale_unit": "м" if description == "Кабель" else "шт",
        "requirements": [requirement] if requirement else []}]})
    with pytest.raises(LLMError, match="invalid_output"):
        _ground(source, request)


def test_grounding_accepts_complete_decimal_property_and_period_separated_positions() -> None:
    from kpdesk.universal.extraction import _ground
    source = "Насос 1 шт, точность 12.5. Кабель 2.5 м, марка АБ-12"
    request = UniversalRequest.model_validate({"lines": [
        {"description": "Насос", "source_quote": "Насос 1 шт, точность 12.5", "quantity": "1", "sale_unit": "шт",
         "requirements": [{"name": "точность", "value": 12.5, "source_quote": "точность 12.5"}]},
        {"description": "Кабель", "source_quote": "Кабель 2.5 м, марка АБ-12", "quantity": "2.5", "sale_unit": "м",
         "requirements": [{"name": "марка", "value": "АБ-12", "source_quote": "марка АБ-12"}]}]})
    _ground(source, request)


@pytest.mark.parametrize("quote", ["цвет синий.", "цвет синий"])
def test_grounding_accepts_final_sentence_period_without_changing_text_value(quote: str) -> None:
    from kpdesk.universal.extraction import _ground
    source = "Насос 1 шт, цвет синий."
    request = UniversalRequest.model_validate({"lines": [{"description": "Насос",
        "source_quote": source, "quantity": 1, "sale_unit": "шт", "requirements": [
            {"name": "цвет", "value": "синий", "source_quote": quote}]}]})
    _ground(source, request)


def test_grounding_preserves_dotted_property_codes_with_terminal_period() -> None:
    from kpdesk.universal.extraction import _ground
    source = "Насос АБ.12 1 шт, марка АБ.12."
    good = {"description": "Насос АБ.12", "model": "АБ.12", "source_quote": source,
            "quantity": 1, "sale_unit": "шт", "requirements": [
                {"name": "марка", "value": "АБ.12", "source_quote": "марка АБ.12."}]}
    _ground(source, UniversalRequest.model_validate({"lines": [good]}))
    bad = {**good, "requirements": [{"name": "марка", "value": "АБ", "source_quote": "марка АБ"}]}
    with pytest.raises(LLMError):
        _ground(source, UniversalRequest.model_validate({"lines": [bad]}))


def test_grounding_cannot_duplicate_one_position_using_terminal_period_variants() -> None:
    from kpdesk.universal.extraction import _ground
    source = 'Насос 1 шт.'
    request = UniversalRequest.model_validate({'lines':[
        {'description':'Насос','source_quote':quote,'quantity':1,'sale_unit':'шт'}
        for quote in ('Насос 1 шт.','Насос 1 шт')]})
    with pytest.raises(LLMError,match='invalid_output'):
        _ground(source,request)


@pytest.mark.parametrize('separator', ['; ', '\n', '. '])
@pytest.mark.parametrize('returned', ['first', 'last', 'both'])
def test_grounding_requires_every_source_position(separator: str, returned: str) -> None:
    from kpdesk.universal.extraction import _ground

    positions: list[dict[str, object]] = [
        {'description': 'Насос', 'source_quote': 'Насос 1 шт', 'quantity': 1, 'sale_unit': 'шт'},
        {'description': 'Кабель', 'source_quote': 'Кабель 2,5 м', 'quantity': '2.5', 'sale_unit': 'м'},
    ]
    source = separator.join(str(p['source_quote']) for p in positions)
    chosen = positions if returned == 'both' else [positions[0 if returned == 'first' else 1]]
    request = UniversalRequest.model_validate({'lines': chosen})
    if returned == 'both':
        _ground(source, request)
    else:
        with pytest.raises(LLMError, match='invalid_output'):
            _ground(source, request)


def test_grounding_cannot_collapse_repeated_source_positions() -> None:
    from kpdesk.universal.extraction import _ground

    source = 'Насос 1 шт; Насос 1 шт'
    request = UniversalRequest.model_validate({'lines': [
        {'description': 'Насос', 'source_quote': 'Насос 1 шт', 'quantity': 1, 'sale_unit': 'шт'},
    ]})
    with pytest.raises(LLMError, match='invalid_output'):
        _ground(source, request)


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
async def test_extraction_transport_does_not_return_an_omitted_position(complete: bool) -> None:
    catalog = CatalogSnapshot(version_id=uuid4(), source_sha256='a' * 64, products=(
        UniversalProduct(sku='P', name='Насос', sale_unit='шт', price_rub=Decimal(100)),))
    positions: list[dict[str, object]] = [
        {'description': 'Насос', 'source_quote': 'Насос 1 шт', 'quantity': 1, 'sale_unit': 'шт'},
        {'description': 'Кабель', 'source_quote': 'Кабель 2,5 м', 'quantity': '2.5', 'sale_unit': 'м'},
    ]
    source = 'Насос 1 шт; Кабель 2,5 м;\n'

    async def inference(*args: object, **kwargs: object) -> object:
        return kwargs['parse'](json.dumps({'lines': positions if complete else positions[:1]}))  # type: ignore[operator]

    with patch('kpdesk.universal.extraction.structured_inference', new=AsyncMock(side_effect=inference)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500))) as client:
            if complete:
                result = await extract_request(source, catalog, LLMSettings(api_key='offline-dummy'), client)
                assert len(result.lines) == 2
            else:
                with pytest.raises(LLMError, match='invalid_output'):
                    await extract_request(source, catalog, LLMSettings(api_key='offline-dummy'), client)


@pytest.mark.parametrize('source,description,quantity,sale_unit,requirements', [
    ('Не нужен Насос 1 шт', 'Насос', None, None, []),
    ('Исключить Насос, цвет синий', 'Насос', None, None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('Насос 1 шт Кабель 2 м', 'Насос', None, None, []),
    ('Насос 2 шт, цвет синий', 'Насос', None, 'шт',
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('Насос 2 шт, цвет синий', 'Насос', None, None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('2 шт: Насос, цвет синий', 'Насос', None, None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('Насос АБ-12, цвет синий', 'Насос', None, None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('Насос 2 шт, цвет синий', 'Насос', '2', None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('2 м: Кабель, цвет синий', 'Кабель', '2', None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
    ('Насос около 2 шт, цвет синий', 'Насос', None, None,
     [{'name': 'цвет', 'value': 'синий', 'source_quote': 'цвет синий'}]),
])
def test_grounding_rejects_omitted_quantity_omitted_unit_negation_and_residual_with_nulls(
    source: str, description: str, quantity: str | None, sale_unit: str | None,
    requirements: list[dict[str, object]],
) -> None:
    from kpdesk.universal.extraction import _ground

    request = UniversalRequest.model_validate({'lines': [{
        'description': description, 'source_quote': source,
        'quantity': quantity, 'sale_unit': sale_unit,
        'requirements': requirements,
    }]})
    with pytest.raises(LLMError, match='invalid_output'):
        _ground(source, request)


def test_grounding_accepts_missing_or_ambiguous_quantity_and_routes_to_manager_review() -> None:
    from kpdesk.universal.extraction import _ground
    from kpdesk.universal.matching import match_request

    source = (
        'Кабель ВВГ 12,5 м, сечение не менее 2.5 мм\n'
        'Насос АБ.12 2-3 шт, напор не более 40 м\n'
        'Клапан обратный около 4 штук, материал латунь\n'
        'Фильтр сетчатый, материал сталь\n'
        'Манометр 2, резьба G1/2'
    )
    request = UniversalRequest.model_validate({'lines': [
        {'description': 'Кабель ВВГ', 'source_quote': 'Кабель ВВГ 12,5 м, сечение не менее 2.5 мм',
         'quantity': '12.5', 'sale_unit': 'м',
         'requirements': [{'name': 'сечение', 'value': 2.5, 'unit': 'мм', 'operator': 'min',
                           'source_quote': 'сечение не менее 2.5 мм'}]},
        {'description': 'Насос АБ.12', 'model': 'АБ.12',
         'source_quote': 'Насос АБ.12 2-3 шт, напор не более 40 м',
         'quantity': None, 'sale_unit': 'шт',
         'requirements': [{'name': 'напор', 'value': 40, 'unit': 'м', 'operator': 'max',
                           'source_quote': 'напор не более 40 м'}]},
        {'description': 'Клапан обратный', 'source_quote': 'Клапан обратный около 4 штук, материал латунь',
         'quantity': None, 'sale_unit': 'шт',
         'requirements': [{'name': 'материал', 'value': 'латунь', 'source_quote': 'материал латунь'}]},
        {'description': 'Фильтр сетчатый', 'source_quote': 'Фильтр сетчатый, материал сталь',
         'quantity': None, 'sale_unit': None,
         'requirements': [{'name': 'материал', 'value': 'сталь', 'source_quote': 'материал сталь'}]},
        {'description': 'Манометр', 'source_quote': 'Манометр 2, резьба G1/2',
         'quantity': '2', 'sale_unit': None,
         'requirements': [{'name': 'резьба', 'value': 'G1/2', 'source_quote': 'резьба G1/2'}]},
    ]})
    _ground(source, request)
    catalog = CatalogSnapshot(version_id=uuid4(), source_sha256='b' * 64, products=(
        UniversalProduct(sku='M1', name='Манометр', sale_unit='шт', price_rub=Decimal(500)),
        UniversalProduct(sku='P1', name='Насос АБ.12', model='АБ.12', sale_unit='шт', price_rub=Decimal(3000)),
    ))
    preview = match_request(request, catalog)
    assert preview['lines'][1]['candidates'][0]['status'] == 'needs_review'
    assert 'quantity_missing' in preview['lines'][1]['candidates'][0]['reason_codes']
    assert preview['lines'][4]['candidates'][0]['status'] == 'needs_review'
    assert 'unit_missing' in preview['lines'][4]['candidates'][0]['reason_codes']


@pytest.mark.parametrize('quantity_text', ['не менее 2 шт', 'не более 3 шт',
                                          'не менее 2 штук', 'не более 2,5 шт'])
def test_bounded_purchase_quantity_stays_unknown_until_manager_review(quantity_text: str) -> None:
    from kpdesk.universal.extraction import _ground
    from kpdesk.universal.matching import match_request

    source = 'Насос ' + quantity_text
    request = UniversalRequest.model_validate({'lines': [{
        'description': 'Насос', 'source_quote': source, 'quantity': None, 'sale_unit': 'шт',
    }]})
    _ground(source, request)
    catalog = CatalogSnapshot(version_id=uuid4(), source_sha256='a' * 64, products=(
        UniversalProduct(sku='P', name='Насос', sale_unit='шт', price_rub=Decimal(100)),))
    candidate = match_request(request, catalog)['lines'][0]['candidates'][0]
    assert candidate['status'] == 'needs_review'
    assert 'quantity_missing' in candidate['reason_codes']


@pytest.mark.parametrize('identity', ['Насос не менее 2 шт', 'Не нужен Насос не менее 2 шт',
                                     'Исключить Насос не более 3 шт'])
def test_bounded_quantities_never_become_exact_or_override_negation(identity: str) -> None:
    from kpdesk.universal.extraction import _ground

    request = UniversalRequest.model_validate({'lines': [{
        'description': 'Насос', 'source_quote': identity, 'quantity': 2, 'sale_unit': 'шт',
    }]})
    with pytest.raises(LLMError, match='invalid_output'):
        _ground(identity, request)
