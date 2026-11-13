"""Safety rules for ranking and trusted local ML artifacts."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from test_ml_training import labeled_queries, product

from kpdesk.matching.artifact import (
    TrustedArtifactError,
    TrustedModel,
    load_trusted_artifact,
    save_trusted_artifact,
)
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.rank import rank_candidates
from kpdesk.matching.retrieve import retrieve_candidates
from kpdesk.matching.schema import Candidate, CatalogProduct, ConstraintResult
from kpdesk.matching.training import TrainingConfig, split_groups, train_reranker


def test_trained_reranker_retains_deterministic_constraints_and_no_fallback() -> None:
    examples = labeled_queries()
    products = {
        "paper-white": product("paper-white", "Бумага A4 80 белая"),
        "paper-color": product("paper-color", "Бумага A4 80 цветная"),
    }
    training = train_reranker(
        split_groups(examples, seed=11), TrainingConfig(products=products, seed=11)
    )
    artifact = TrustedModel(
        pipeline=training.pipeline,
        feature_schema=FEATURE_SCHEMA,
        dataset_sha256="0" * 64,
    )
    request = examples[0].request
    candidates = retrieve_candidates(request, list(products.values()))
    scored = rank_candidates(request, candidates, mode="ml", artifact=artifact)
    assert {item.product.sku for item in scored} == set(products)
    assert all(0 <= item.score <= 1 for item in scored)
    assert scored == rank_candidates(request, candidates, mode="ml", artifact=artifact)

    with pytest.raises(ValueError, match="model_unavailable"):
        rank_candidates(request, candidates, mode="ml", artifact=None)

    bad_candidate = Candidate(
        product=CatalogProduct(
            sku="bad-gsm", name="Бумага А4 65",
            category="paper", sale_unit="пачка", pieces_per_sale_unit=1,
            price_rub=Decimal("10.00"), paper_format="A4", paper_gsm=65,
        ),
        constraints=ConstraintResult("incompatible", ("paper_gsm_mismatch",)),
        score=0.99,
    )
    assert all(
        item.product.sku != "bad-gsm"
        for item in rank_candidates(request, [*candidates, bad_candidate], "ml", artifact)
    )


def test_tf_idf_baseline_is_available_without_trained_model() -> None:
    examples = labeled_queries()
    candidate_products = [
        product("paper-white", "Бумага A4 80 белая"),
        product("paper-color", "Бумага A4 80 цветная"),
    ]
    cands = retrieve_candidates(examples[0].request, candidate_products)
    result = rank_candidates(examples[0].request, cands, "tfidf", None)
    assert len(result) == 2
    assert result[0].score >= result[1].score
    assert all(0 <= c.score <= 1 for c in result)


def test_joblib_artifact_is_restricted_to_trusted_root_and_hash_checked(
    tmp_path: Path,
) -> None:
    products = {
        "paper-white": product("paper-white", "Бумага A4 80 белая"),
        "paper-color": product("paper-color", "Бумага A4 80 цветная"),
    }
    trained = train_reranker(
        split_groups(labeled_queries(), seed=11), TrainingConfig(products=products)
    )
    artifact = TrustedModel(
        pipeline=trained.pipeline,
        feature_schema=FEATURE_SCHEMA,
        dataset_sha256="a" * 64,
    )
    trusted_root = tmp_path / "trusted"
    output = trusted_root / "model.joblib"
    save_trusted_artifact(output, artifact, trusted_root=trusted_root)
    loaded = load_trusted_artifact(
        output, expected_schema=FEATURE_SCHEMA, trusted_root=trusted_root
    )
    assert loaded.feature_schema == FEATURE_SCHEMA
    assert loaded.dataset_sha256 == "a" * 64
    assert loaded.training_data_kind == "unverified"
    manifest_path = output.with_suffix(".joblib.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["production_eligible"] is False
    assert manifest["training_data_kind"] == "unverified"
    forged = dict(manifest)
    forged["production_eligible"] = True
    manifest_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(TrustedArtifactError, match="production"):
        load_trusted_artifact(output, expected_schema=FEATURE_SCHEMA, trusted_root=trusted_root)
    forged["production_eligible"] = False
    forged["training_data_kind"] = "fabricated_human_gold"
    manifest_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(TrustedArtifactError, match="provenance"):
        load_trusted_artifact(output, expected_schema=FEATURE_SCHEMA, trusted_root=trusted_root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(TrustedArtifactError, match="schema"):
        load_trusted_artifact(
            output, expected_schema="unknown-schema", trusted_root=trusted_root
        )
    with pytest.raises(TrustedArtifactError, match="outside"):
        load_trusted_artifact(
            output, expected_schema=FEATURE_SCHEMA, trusted_root=tmp_path / "another"
        )

    with output.open("ab") as fh:
        fh.write(b"malicious")
    with pytest.raises(TrustedArtifactError, match="SHA"):
        load_trusted_artifact(
            output, expected_schema=FEATURE_SCHEMA, trusted_root=trusted_root
        )
