"""Validate the real provider schema with the standard JSON Schema validator."""
import json
from functools import lru_cache
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator

from kpdesk.extraction.groq import _parse_category_request, _structured_schema
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES


def line(category: str | None, attributes: object) -> dict[str, object]:
    return {
        'source_quote': 'Учебная строка', 'description': 'Учебный товар',
        'category': category, 'quantity': None, 'unit': None, 'brand': None,
        'paper_format': None, 'paper_gsm': None, 'pen_ink_color': None, 'pen_ink_type': None,
        'attributes': attributes,
    }


@lru_cache(maxsize=1)
def validator() -> Validator:
    schema = _structured_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid(row: dict[str, object]) -> bool:
    return validator().is_valid(json.loads(json.dumps({'lines': [row], 'instruction_quotes': []})))


def test_actual_live_wrong_marker_property_cannot_be_emitted_by_schema() -> None:
    assert not valid(line('marker', [
        {'name': 'marker_type', 'value': 'перманентный'}, {'name': 'color', 'value': 'черный'},
    ]))
    assert valid(line('marker', [{'name': 'marker.ink_color', 'value': 'черный'},
                                 {'name': 'marker.marker_type', 'value': 'перманентный'}]))
    assert not valid(line('marker', [{'name': 'marker.color', 'value': 'черный'}]))


@pytest.mark.parametrize('category', CATEGORY_ATTRIBUTES)
def test_each_category_maps_its_qualified_typed_attributes(category: str) -> None:
    attributes = [{'name': category + '.' + rule.name,
                   'value': 2 if rule.value_kind is int else ['Учебная модель']
                   if rule.value_kind is list else 'Учебное'} for rule in CATEGORY_ATTRIBUTES[category]]
    assert valid(line(category, []))
    assert valid(line(category, attributes))
    result = _parse_category_request(json.dumps({'lines': [line(category, attributes)], 'instruction_quotes': []}))
    assert set(result.lines[0].attributes) == {r.name for r in CATEGORY_ATTRIBUTES[category]}
    assert not valid(line(category, [{'name': category + '.invented_property', 'value': 'x'}]))


def test_category_specific_types_and_inapplicable_top_level_facts() -> None:
    assert valid(line('mouse', [{'name': 'mouse.dpi', 'value': 1600}]))
    assert not valid(line('mouse', [{'name': 'mouse.dpi', 'value': '1600'}]))
    assert not valid(line('mouse', [{'name': 'mouse.dpi', 'value': True}]))
    assert valid(line(None, []))
    for row in ({**line('marker', []), 'paper_gsm': 80},
                line('marker', [{'name': 'toner_cartridge.color', 'value': 'черный'}]),
                line(None, [{'name': 'marker.ink_color', 'value': 'черный'}])):
        # The wire limits names/types; the domain enforces their line category.
        assert valid(row)
        with pytest.raises(ValueError):
            _parse_category_request(json.dumps({'lines': [row], 'instruction_quotes': []}))


def test_cartridge_compatibility_is_a_nullable_string_array() -> None:
    assert valid(line('toner_cartridge', [{'name': 'toner_cartridge.compatible_models', 'value': ['HP LaserJet 1010']}]))
    assert not valid(line('toner_cartridge', [{'name': 'toner_cartridge.compatible_models', 'value': 'HP LaserJet 1010'}]))


def test_schema_is_closed_and_all_refs_are_local() -> None:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get('type') == 'object':
                assert value['additionalProperties'] is False
                assert value['properties']
                assert set(value['required']) == set(value['properties'])
            assert '$ref' not in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(_structured_schema())
