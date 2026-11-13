"""Model identifiers are source evidence, never a general range exception."""
import pytest

from kpdesk.extraction.grounding import validate_extraction_grounding
from kpdesk.extraction.schema import ExtractedRequest


def cartridge(source: str, models: list[str], quantity: int = 2) -> ExtractedRequest:
    return ExtractedRequest.model_validate({'lines': [{
        'source_quote': source, 'description': 'Картридж', 'category': 'toner_cartridge',
        'quantity': quantity, 'unit': 'штука', 'brand': None,
        'paper_format': None, 'paper_gsm': None, 'pen_ink_color': None, 'pen_ink_type': None,
        'attributes': {'compatible_models': models},
    }]})


@pytest.mark.parametrize('model', ['HP LaserJet 1020', 'Canon LBP 6030', 'Brother HL-1110'])
def test_exact_model_identifier_before_dash_is_not_a_quantity_range(model: str) -> None:
    source = f'Картридж для {model} — 2 штуки'
    assert validate_extraction_grounding(source, cartridge(source, [model])) == ()


@pytest.mark.parametrize(('source', 'models', 'quantity'), [
    ('Картридж — 10 — 20 штук', ['10'], 20),
    ('Картридж от 10 — 20 штук', ['от 10'], 20),
    ('Картридж для HP LaserJet 1020 — 1,5 штуки', ['HP LaserJet 1020'], 5),
    ('Картридж для HP LaserJet 1020 — 2–4 штуки', ['HP LaserJet 1020'], 4),
    ('Картридж для HP LaserJet 1020 — -2 штуки', ['HP LaserJet 1020'], 2),
    ('Картридж — 1020 — 2 штуки', ['HP LaserJet 1020'], 2),
])
def test_range_decimal_negative_or_fabricated_identifier_still_blocks(
    source: str, models: list[str], quantity: int,
) -> None:
    assert validate_extraction_grounding(source, cartridge(source, models, quantity))


def test_cropped_quote_cannot_borrow_model_evidence_outside_its_boundary() -> None:
    source = 'Картридж для HP LaserJet 1020 — 2 штуки'
    assert validate_extraction_grounding(source, cartridge('2 штуки', ['HP LaserJet 1020']))
