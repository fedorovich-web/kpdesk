"""Explicit manager decisions become scoped, price-free training labels."""
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_document_alternatives import saved

from kpdesk.matching.feedback_bundle import (
    FeedbackBundle,
    FeedbackDecision,
    FeedbackExportRequest,
    build_feedback_bundle,
    dataset_from_feedback,
    feedback_context,
)


def bundle() -> FeedbackBundle:
    quote, products = saved()
    request = FeedbackExportRequest(expected_revision=quote.revision,
        source_snapshot_sha256=quote.snapshot_sha256, reviewed=True, training_consent=True,
        decisions=(FeedbackDecision(line_number=1, relevant_sku='FIRST', rejected_skus=('SECOND',), no_match=False),))
    return build_feedback_bundle(quote, products, request, organization_id=uuid4(),
                                 reviewer_id=uuid4(), reviewed_at=datetime(2026, 11, 2, tzinfo=UTC))


def test_export_contains_only_explicit_decisions_and_no_prices_or_document() -> None:
    result = bundle()
    assert result.format == 'kpdesk-manager-feedback-v1'
    assert result.training_data_kind == 'manager_reviewed'
    assert [event.verdict for event in result.events] == ['relevant', 'irrelevant']
    assert all(p.price_rub == 0 for p in result.catalog)
    text = result.model_dump_json()
    assert 'commercial_terms' not in text and 'company_profile' not in text
    dataset = dataset_from_feedback([result])
    assert dataset['provenance']['kind'] == 'manager_reviewed'
    assert dataset['queries'][0]['relevant_skus'] == ['FIRST']
    assert dataset['queries'][0]['reviewed_negative_skus'] == ['SECOND']


@pytest.mark.parametrize('changes', [
    {'reviewed': False}, {'training_consent': False}, {'expected_revision': 99},
    {'source_snapshot_sha256': 'b' * 64},
])
def test_unreviewed_unconsented_or_stale_export_fails(changes: dict[str, object]) -> None:
    quote, products = saved()
    values = {'expected_revision': quote.revision, 'source_snapshot_sha256': quote.snapshot_sha256,
              'reviewed': True, 'training_consent': True, 'decisions': [
                  {'line_number': 1, 'relevant_sku': 'FIRST', 'rejected_skus': ['SECOND'], 'no_match': False}]}
    values.update(changes)
    request = FeedbackExportRequest.model_validate(values)
    with pytest.raises(ValueError):
        build_feedback_bundle(quote, products, request, organization_id=uuid4(), reviewer_id=uuid4())


def test_unknown_or_incompatible_positive_is_not_a_training_label() -> None:
    quote, products = saved()
    wrong = products[1].model_copy(update={'attributes': {**products[1].attributes, 'lead_grade': '2B'}})
    request = FeedbackExportRequest(expected_revision=quote.revision, source_snapshot_sha256=quote.snapshot_sha256,
        reviewed=True, training_consent=True, decisions=(FeedbackDecision(
            line_number=1, relevant_sku='SECOND', rejected_skus=('FIRST',), no_match=False),))
    with pytest.raises(ValueError):
        build_feedback_bundle(quote, [products[0], wrong], request, organization_id=uuid4(), reviewer_id=uuid4())


def test_exports_cannot_mix_tenants_or_catalogue_versions_and_replays_do_not_reweight() -> None:
    result = bundle()
    assert len(dataset_from_feedback([result, result])['queries']) == 1
    other = bundle()
    with pytest.raises(ValueError):
        dataset_from_feedback([result, other])


def test_no_match_and_positive_are_mutually_exclusive_and_negatives_are_explicit() -> None:
    with pytest.raises(ValueError):
        FeedbackDecision(line_number=1, relevant_sku='FIRST', rejected_skus=('SECOND',), no_match=True)
    with pytest.raises(ValueError):
        FeedbackDecision(line_number=1, relevant_sku='FIRST', rejected_skus=(), no_match=False)
    result = bundle()
    assert len(json.loads(result.model_dump_json())['events']) == 2


def test_no_match_requires_explicit_review_of_entire_catalogue() -> None:
    quote, products = saved()
    partial = FeedbackDecision(line_number=1, relevant_sku=None, rejected_skus=('SECOND',), no_match=True)
    request = FeedbackExportRequest(expected_revision=quote.revision, source_snapshot_sha256=quote.snapshot_sha256,
                                    reviewed=True, training_consent=True, decisions=(partial,))
    with pytest.raises(ValueError, match='full_catalog'):
        build_feedback_bundle(quote, products, request,
                              organization_id=uuid4(), reviewer_id=uuid4())
    complete = partial.model_copy(update={'rejected_skus': ('FIRST', 'SECOND')})
    result = build_feedback_bundle(quote, products, request.model_copy(update={'decisions': (complete,)}),
                                    organization_id=uuid4(), reviewer_id=uuid4())
    assert dataset_from_feedback([result])['queries'][0]['relevant_skus'] == []


def test_partial_candidate_list_never_offers_global_no_match() -> None:
    quote, products = saved()
    products += [products[0].model_copy(update={'sku': f'EXTRA-{i}'}) for i in range(20)]
    context = feedback_context(quote, products)
    assert not context['lines'][0]['no_match_allowed']
    assert len(context['lines'][0]['candidates']) <= 20


def test_candidate_review_exposes_actual_properties_even_when_names_match() -> None:
    quote, products = saved()
    products[1] = products[1].model_copy(update={'attributes': {**products[1].attributes, 'lead_grade': '2B'}})
    candidate = next(p for p in feedback_context(quote, products)['lines'][0]['candidates'] if p['sku'] == 'SECOND')
    check = next(c for c in candidate['requirements'] if c['field'] == 'lead_grade')
    assert check['requested'] == 'HB' and check['actual'] == '2B' and check['status'] == 'mismatch'


@pytest.mark.parametrize('conflict', [False, True])
def test_quote_copies_do_not_reweight_the_same_request_and_conflicts_are_visible(conflict: bool) -> None:
    quote, products = saved()
    org, actor = uuid4(), uuid4()
    bundles = []
    for index in range(2):
        current = replace(quote, quote_id=uuid4(), revision=index + 1, snapshot_sha256=str(index) * 64)
        positive = 'SECOND' if conflict and index else 'FIRST'
        negative = 'FIRST' if positive == 'SECOND' else 'SECOND'
        body = FeedbackExportRequest(expected_revision=current.revision,
            source_snapshot_sha256=current.snapshot_sha256, reviewed=True, training_consent=True,
            decisions=(FeedbackDecision(line_number=1, relevant_sku=positive, rejected_skus=(negative,), no_match=False),))
        bundles.append(build_feedback_bundle(current, products, body, organization_id=org, reviewer_id=actor))
    if conflict:
        with pytest.raises(ValueError, match='conflict'):
            dataset_from_feedback(bundles)
    else:
        data = dataset_from_feedback(bundles)
        assert len(data['queries']) == 1
        assert data['provenance']['source_query_count'] == 2
