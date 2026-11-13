"""Observed evidence pooling and missing no-match accounting are real regressions."""
import json
from decimal import Decimal

import pytest
from test_ml_training import product

from kpdesk.extraction.grounding import validate_extraction_grounding
from kpdesk.extraction.schema import ExtractedLine, ExtractedRequest
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.metrics import evaluate_ranking
from kpdesk.matching.schema import CatalogProduct, EvaluationCase, RequestedItem
from kpdesk.quotes.document import ProposalNarrative
from kpdesk.quotes.narrative_review import _context_issues


def test_no_match_suggestions_are_counted_instead_of_discarded() -> None:
    result = evaluate_ranking([
        EvaluationCase('positive', frozenset({'A'})), EvaluationCase('negative', frozenset()),
        EvaluationCase('empty', frozenset()),
    ], {'positive': ['B', 'A'], 'negative': ['B'], 'empty': []}, k=3)
    assert result.hit_at_1 == 0 and result.hit_at_3 == 1
    assert result.false_suggestions_on_no_match == 1
    assert result.correct_empty_no_match == 1


def test_duplicate_predictions_do_not_inflate_quality_measurements() -> None:
    with pytest.raises(ValueError, match='Duplicate predicted SKU'):
        evaluate_ranking([EvaluationCase('q', frozenset({'A'}))], {'q': ['A', 'A']})


def marker_facts() -> str:
    return json.dumps({'products': [
        {'name': 'Маркер для досок синий', 'verified_specs': {'category': 'marker', 'marker_type': 'whiteboard'}},
        {'name': 'Маркер перманентный черный', 'verified_specs': {'category': 'marker', 'marker_type': 'permanent'}},
    ]})


@pytest.mark.parametrize('claim', [
    'Маркер перманентный черный подходит для досок.',
    'Все маркеры подходят для досок.',
    'Маркеры подходят для досок.',
    'Перманентный маркер подходит для досок.',
    'Оба маркера — это маркеры для досок.',
    'Маркер для досок синий — для записей, а маркер перманентный черный подходит для досок.',
    'Маркер перманентный черный. Предназначен для маркировки документов.',
    'Маркер перманентный черный и Маркер для досок синий подходят для досок.',
])
def test_markers_cannot_borrow_board_compatibility_from_another_sku(claim: str) -> None:
    draft = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
        solution=claim, closing='Подтвердите состав.')
    issues = _context_issues(marker_facts(), draft)
    assert any(issue['reason'] == 'unsupported_surface' and issue['section'] == 'solution' for issue in issues)


def test_each_marker_keeps_its_own_supported_ordinary_purpose() -> None:
    draft = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
        solution='Маркер для досок синий предназначен для досок, а маркер перманентный черный — для стойкой маркировки.',
        closing='Подтвердите состав.')
    assert _context_issues(marker_facts(), draft) == []


def test_supported_purpose_can_precede_its_explicit_product_name() -> None:
    draft = ProposalNarrative(headline='КП', opening='Предлагаем рассмотреть состав.',
        solution='Для досок предлагаем Маркер для досок синий, а для стойкой маркировки — Маркер перманентный черный.',
        closing='Подтвердите состав.')
    assert _context_issues(marker_facts(), draft) == []


@pytest.mark.parametrize('claim', [
    'В состав включены Маркер для досок синий и Маркер перманентный черный.',
    'Синий маркер для досок — для записей на маркерной доске. Чёрный перманентный маркер — для стойкой маркировки.',
    'Предлагаем состав для печати документов, работы на маркерной доске и стойкой маркировки.',
])
def test_supported_names_and_distinct_marker_roles_are_not_collective_compatibility(claim: str) -> None:
    draft=ProposalNarrative(headline='КП',opening='Предлагаем состав.',solution=claim,closing='Подтвердите состав.')
    assert _context_issues(marker_facts(),draft)==[]


def test_actual_writer_cannot_expand_durable_marking_to_unspecified_surfaces() -> None:
    draft=ProposalNarrative(headline='КП',opening='Предлагаем состав.',
        solution='Чёрный перманентный маркер — для стойкой маркировки на различных поверхностях.',closing='Подтвердите состав.')
    assert any(issue['reason']=='unsupported_surface' for issue in _context_issues(marker_facts(),draft))


@pytest.mark.parametrize('description', [
    'Самоклеящаяся бумага для этикеток A4 плотность 80',
    'Бумага офисная А4 80 г/м2 самоклеящиеся ярлыки',
    'Этикетки самоклеящиеся на листах A4 80 г/м2',
])
def test_explicit_self_adhesive_labels_are_not_ordinary_office_paper(description: str) -> None:
    request = RequestedItem(description=description, category='paper', quantity=2, unit='пачка', paper_format='A4', paper_gsm=80)
    decision = check_constraints(request, product('P', 'Бумага офисная A4 80'))
    assert decision.status == 'incompatible'
    assert 'category_description_mismatch' in decision.reason_codes


@pytest.mark.parametrize('description', ['Стержень для синей шариковой ручки', 'Стержни синие для шариковых ручек'])
def test_a_refill_request_does_not_match_a_complete_pen(description: str) -> None:
    request = RequestedItem(description=description, category='pen', quantity=2, unit='штука', pen_ink_color='синий', pen_ink_type='шариковая')
    pen = CatalogProduct(sku='PEN', name='Ручка шариковая синяя', category='pen', sale_unit='штука',
        pieces_per_sale_unit=1, price_rub=Decimal(10), pen_ink_color='синий', pen_ink_type='шариковая')
    assert check_constraints(request, pen).status == 'incompatible'


@pytest.mark.parametrize('description', [
    'Бумага офисная A4 80 для печати документов',
    'Бумага A4 80, не самоклеящиеся этикетки',
    'Бумага A4 80 без самоклеящихся ярлыков',
])
def test_ordinary_paper_and_explicitly_excluded_labels_keep_their_matching(description: str) -> None:
    request = RequestedItem(description=description, category='paper', quantity=2, unit='пачка', paper_format='A4', paper_gsm=80)
    assert check_constraints(request, product('P', 'Бумага офисная A4 80')).status == 'compatible'


def test_extraction_checks_original_fragment_even_when_model_rewrites_product_kind() -> None:
    source = 'Стержни для шариковых ручек — 2 штуки.'
    result = ExtractedRequest(lines=[ExtractedLine(source_quote=source, description='Ручка шариковая',
        category='pen', quantity=2, unit='штука', brand=None, paper_format=None,
        paper_gsm=None, pen_ink_color=None, pen_ink_type=None)])
    assert 'category_source_conflict' in validate_extraction_grounding(source, result)


@pytest.mark.parametrize('prefix', ['1. ', '2) ', '- ', '• '])
def test_list_prefix_cannot_hide_the_requested_refill_kind(prefix: str) -> None:
    source = prefix + 'Стержни для шариковых ручек — 2 штуки.'
    result = ExtractedRequest(lines=[ExtractedLine(source_quote=source, description='Ручка шариковая',
        category='pen', quantity=2, unit='штука', brand=None, paper_format=None,
        paper_gsm=None, pen_ink_color=None, pen_ink_type=None)])
    assert 'category_source_conflict' in validate_extraction_grounding(source, result)
