"""Independent train/validation/test group splits and genuine scikit-learn fit."""

from decimal import Decimal

import pytest

from kpdesk.matching.features import PairFeatureExtractor, PairInput, build_pair_features
from kpdesk.matching.schema import CatalogProduct, RequestedItem
from kpdesk.matching.training import (
    LabeledQuery,
    TrainingConfig,
    split_groups,
    train_reranker,
)


def product(sku: str, name: str, price: str = "100.00") -> CatalogProduct:
    return CatalogProduct(
        sku=sku, name=name, category="paper", sale_unit="пачка",
        pieces_per_sale_unit=1, price_rub=Decimal(price),
        paper_format="A4", paper_gsm=80,
    )


def labeled_queries() -> list[LabeledQuery]:
    texts = [
        ("q1", "grp-1", "Бумага A4 80 белая", "paper-white"),
        ("q2", "grp-2", "Бумага A4 80 цветная", "paper-color"),
        ("q3", "grp-3", "Белая бумага A4 80", "paper-white"),
        ("q4", "grp-4", "Цветная бумага A4 80", "paper-color"),
        ("q5", "grp-5", "Пачка белая бумага A4", "paper-white"),
        ("q6", "grp-6", "Пачка цветная бумага A4", "paper-color"),
        ("q7", "grp-7", "A4 белая бумага", "paper-white"),
        ("q8", "grp-8", "A4 цветная бумага", "paper-color"),
    ]
    return [
        LabeledQuery(
            query_id=qid,
            query_group_id=group,
            request=RequestedItem(
                description=text, category="paper", quantity=3, unit="пачка",
                paper_format="A4", paper_gsm=80,
            ),
            relevant_skus=frozenset((sku,)),
        )
        for qid, group, text, sku in texts
    ]


def test_pair_features_exclude_price_and_preserve_exact_pack_semantics() -> None:
    query = labeled_queries()[0].request
    a = product("paper-white", "Бумага A4 80 белая", "100.00")
    b = product("paper-white", "Бумага A4 80 белая", "999.99")
    assert build_pair_features(query, a) == build_pair_features(query, b)


def test_vectorizer_fits_only_training_pair_text() -> None:
    train_request = labeled_queries()[0].request
    unusual_request = train_request.model_copy(update={"description": "zzxqvneologism"})
    item = product("paper-white", "Бумага A4 80 белая")
    features = PairFeatureExtractor()
    features.fit([PairInput(train_request, item)])
    before = dict(features.vectorizer_.vocabulary_)
    transformed = features.transform([PairInput(unusual_request, item)])
    assert transformed.shape[0] == 1
    assert features.vectorizer_.vocabulary_ == before
    assert "zzxq" not in before


def test_group_split_is_deterministic_and_has_no_leakage() -> None:
    queries = labeled_queries()
    queries.append(
        LabeledQuery(
            query_id="q1-paraphrase", query_group_id="grp-1",
            request=queries[0].request.model_copy(
                update={"description": "Белая бумага офисная A4 80"}
            ),
            relevant_skus=frozenset(("paper-white",)),
        )
    )
    a = split_groups(queries, seed=42)
    b = split_groups(queries, seed=42)
    assert a == b
    train_groups = {q.query_group_id for q in a.train}
    valid_groups = {q.query_group_id for q in a.validation}
    test_groups = {q.query_group_id for q in a.test}
    assert train_groups and valid_groups and test_groups
    assert not (train_groups & valid_groups)
    assert not (valid_groups & test_groups)
    assert not (train_groups & test_groups)
    assert sum(map(len, (a.train, a.validation, a.test))) == len(queries)


def test_training_produces_actual_predict_proba_with_known_labels() -> None:
    products = {
        "paper-white": product("paper-white", "Бумага A4 80 белая"),
        "paper-color": product("paper-color", "Бумага A4 80 цветная"),
    }
    split = split_groups(labeled_queries(), seed=12)
    trained = train_reranker(split, TrainingConfig(products=products, seed=12))
    assert trained.train_pairs >= 2
    assert set(trained.pipeline.named_steps) == {"pair_features", "classifier"}
    assert hasattr(trained.pipeline.named_steps["classifier"], "coef_")
    scores = trained.pipeline.predict_proba([
        PairInput(labeled_queries()[0].request, products["paper-white"]),
        PairInput(labeled_queries()[0].request, products["paper-color"]),
    ])
    assert scores.shape == (2, 2)
    assert all(0 <= float(row[1]) <= 1 for row in scores)
    assert trained.validation_metrics.total_queries == len(split.validation)


def test_small_dataset_or_duplicate_query_ids_are_rejected() -> None:
    queries = labeled_queries()
    with pytest.raises(ValueError, match="groups"):
        split_groups(queries[:2], seed=2)
    with pytest.raises(ValueError, match="Duplicate"):
        split_groups([queries[0], queries[0], *queries[1:]], seed=2)


def test_cross_group_duplicate_text_is_rejected_before_training() -> None:
    examples = labeled_queries()
    duplicated = examples[0].request.model_copy(
        update={"description": "  БУМАГА    A4 80 БЕЛАЯ  "}
    )
    same_text_different_group = LabeledQuery(
        query_id="q-new",
        query_group_id="grp-other",
        request=duplicated,
        relevant_skus=frozenset(("paper-white",)),
    )
    with pytest.raises(ValueError, match="cross.group|leakage"):
        split_groups([*examples, same_text_different_group], seed=42)
