from kpdesk.matching.metrics import evaluate_ranking
from kpdesk.matching.schema import EvaluationCase


def test_retrieval_metrics_do_not_inject_missing_gold_sku() -> None:
    cases = [
        EvaluationCase(query_id="q1", relevant_skus=frozenset({"A", "B"})),
        EvaluationCase(query_id="q2", relevant_skus=frozenset({"C"})),
        EvaluationCase(query_id="q3", relevant_skus=frozenset()),
    ]
    ranked = {
        "q1": ["X", "A", "Y"],
        "q2": ["Z", "Y", "C"],
        "q3": [],
    }
    result = evaluate_ranking(cases, ranked, k=2)
    assert result.positive_queries == 2
    assert result.no_match_queries == 1
    assert result.retrieval_hits == 1
    assert result.hit_at_1 == 0
    assert result.hit_at_3 == 2
    assert result.unretrievable_positive_queries == 1


def test_empty_evaluation_has_no_fabricated_accuracy() -> None:
    result = evaluate_ranking([], {}, k=20)
    assert result.total_queries == 0
    assert result.positive_queries == 0
    assert result.retrieval_hits == 0
