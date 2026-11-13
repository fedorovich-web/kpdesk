"""Saved live failure: correct facts need their quantity in the cited item span."""
import json
from pathlib import Path

import pytest

from kpdesk.extraction.grounding import validate_extraction_grounding
from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest
from kpdesk.extraction.service import _parse_category_request


def captured() -> tuple[str, ExtractedRequest]:
    fixture = json.loads((Path(__file__).parent / 'fixtures/extraction/ruler_source_quote_v12.json')
                         .read_text(encoding='utf-8'))
    return fixture['source'], _parse_category_request(json.dumps(fixture['output'], ensure_ascii=False))


def test_saved_provider_facts_are_correct_but_quote_omits_quantity_evidence() -> None:
    source, result = captured()
    assert result.lines[0].quantity == 2 and result.lines[0].unit == 'штука'
    assert result.lines[0].attributes == {'length_mm': 500}
    assert result.lines[0].source_quote == 'Линейка металлическая 50 см'
    assert validate_extraction_grounding(source, result) == ('line_0:quantity_or_unit_unverified',)


def test_full_evidence_span_accepts_same_facts_without_rewriting_values() -> None:
    source, original = captured()
    # Explicit offline counterfactual, never passed off as a new provider output.
    expanded = original.model_copy(update={'lines':[
        original.lines[0].model_copy(update={'source_quote':source}),
    ]})
    assert validate_extraction_grounding(source, expanded) == ()
    assert expanded.lines[0].attributes == original.lines[0].attributes
    assert expanded.lines[0].quantity == original.lines[0].quantity


def test_count_from_another_item_cannot_validate_the_first_quote() -> None:
    source, original = captured()
    wrong = original.model_copy(update={'lines':[
        original.lines[0].model_copy(update={'source_quote':source,'quantity':5}),
    ]})
    assert validate_extraction_grounding(source+'\nНужно 5 штук: другая линейка.', wrong) == (
        'line_0:quantity_or_unit_unverified',
    )


@pytest.mark.parametrize('prefix', ['Нужно 2 штуки: ', '2 шт.: '])
def test_complete_quantity_prefix_cannot_hide_a_refill_as_a_pen(prefix: str) -> None:
    source = prefix+'Стержни для шариковых ручек.'
    result = ExtractedRequest(lines=[ExtractedLine(source_quote=source,
        description='Ручка шариковая',category='pen',quantity=2,unit='штука',brand=None,
        paper_format=None,paper_gsm=None,pen_ink_color=None,pen_ink_type=None)])
    assert 'category_source_conflict' in validate_extraction_grounding(source,result)


def test_complete_pen_with_quantity_prefix_keeps_its_valid_source_evidence() -> None:
    source = 'Нужно 2 штуки: Ручка шариковая синяя со стержнем.'
    result = ExtractedRequest(lines=[ExtractedLine(source_quote=source,
        description='Ручка шариковая синяя со стержнем',category='pen',quantity=2,unit='штука',
        brand=None,paper_format=None,paper_gsm=None,pen_ink_color='синяя',pen_ink_type='шариковая')])
    assert validate_extraction_grounding(source,result) == ()
