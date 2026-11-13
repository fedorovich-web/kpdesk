"""Numeric/format facts must not contradict or appear outside their source quote."""
import pytest

from kpdesk.extraction.grounding import validate_extraction_grounding
from kpdesk.extraction.schema import ExtractedRequest


def result(source: str, *, gsm: int | None, paper_format: str | None = 'A4') -> ExtractedRequest:
    return ExtractedRequest.model_validate({'lines': [{
        'source_quote': source, 'description': 'Бумага', 'category': 'paper',
        'quantity': None, 'unit': None, 'brand': None, 'paper_format': paper_format,
        'paper_gsm': gsm, 'pen_ink_color': None, 'pen_ink_type': None, 'attributes': {},
    }]})


@pytest.mark.parametrize(('source', 'gsm', 'paper_format'), [
    ('Бумага А4 80 г/м²', 120, 'A4'),
    ('Бумага А4 80 г/м², нужно 120 пачек', 120, 'A4'),
    ('Бумага А4, нужно 80 пачек', 80, 'A4'),
    ('Бумага А4 80 г/м²', 80, 'A3'),
    ('Бумага А40 80 г/м²', 80, 'A4'),
    ('Бумага без характеристик', 80, 'A4'),
])
def test_unmentioned_or_conflicting_paper_facts_require_review(
    source: str, gsm: int, paper_format: str,
) -> None:
    assert validate_extraction_grounding(source, result(source, gsm=gsm, paper_format=paper_format))


@pytest.mark.parametrize('density', ['80 г/м²', '80 г/м2', '80 г/м^2', '80 g/m2', '80 gsm', 'плотность: 80'])
def test_supported_density_notations_and_cyrillic_format_are_evidence(density: str) -> None:
    source = 'Бумага А4, ' + density
    assert validate_extraction_grounding(source, result(source, gsm=80)) == ()


def test_unknown_paper_facts_stay_unknown() -> None:
    source = 'Бумага без характеристик'
    assert validate_extraction_grounding(source, result(source, gsm=None, paper_format=None)) == ()


@pytest.mark.parametrize(('source', 'quote', 'gsm', 'paper_format'), [
    ('Бумага А4 180 г/м²', '80 г/м²', 80, None),
    ('Бумага А4 80 г/м20', '80 г/м2', 80, None),
    ('Бумага А40 80 г/м²', 'А4', None, 'A4'),
    ('Бумага А4', '4', None, 'A4'),
])
def test_cropped_quote_cannot_hide_numeric_or_format_boundaries(
    source: str, quote: str, gsm: int | None, paper_format: str | None,
) -> None:
    assert validate_extraction_grounding(source, result(quote, gsm=gsm, paper_format=paper_format))


def test_one_source_occurrence_cannot_be_reused_to_duplicate_order_lines() -> None:
    source = 'Бумага А4 80 г/м²'
    extracted = result(source, gsm=80)
    extracted = extracted.model_copy(update={'lines': extracted.lines * 2})
    assert validate_extraction_grounding(source, extracted)
    assert validate_extraction_grounding(source + '\n' + source, extracted) == ()
