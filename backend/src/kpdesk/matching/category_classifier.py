"""Independent trained request-category classifier, with abstention and trusted artifacts.

Not an implicit category override. Consumers must explicitly accept suggestions.
Classifier training uses synthetic grouped requests; production is not enabled.
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import joblib
import numpy as np
import sklearn
from sklearn.base import TransformerMixin
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.constraints import description_category_conflict
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES, TAXONOMY_VERSION
from kpdesk.matching.schema import RequestedItem
from kpdesk.matching.training import LabeledQuery

TRUSTED_MODEL_ROOT = PROJECT_ROOT / "ml" / "models"
MODEL_FORMAT = "kpdesk-request-category-v1"


@dataclass(frozen=True, slots=True)
class CategoryDatasetSplit:
    train: tuple[LabeledQuery, ...]
    validation: tuple[LabeledQuery, ...]
    test: tuple[LabeledQuery, ...]


@dataclass(frozen=True, slots=True)
class CategorySuggestion:
    predicted: str | None
    best_category: str
    confidence: float
    margin: float
    threshold: float
    abstained: bool


@dataclass(frozen=True, slots=True)
class CategoryClassifier:
    pipeline: Pipeline
    threshold: float
    min_margin: float
    dataset_sha256: str
    split_seed: int


def stratified_category_groups(
    records: Sequence[LabeledQuery], seed: int = 42,
) -> CategoryDatasetSplit:
    """Hold out complete SKU/intent groups, with every category in train/val/test."""
    by_category: dict[str, dict[str, list[LabeledQuery]]] = defaultdict(lambda: defaultdict(list))
    groups: dict[str, str] = {}
    seen_ids: set[str] = set()
    descriptions: dict[str, str] = {}
    for record in records:
        category = record.request.category
        if category not in CATEGORY_ATTRIBUTES or not record.query_id or not record.query_group_id:
            raise ValueError("Invalid typed category or query/group identity.")
        if record.query_id in seen_ids:
            raise ValueError("Duplicate request identity across category corpus.")
        seen_ids.add(record.query_id)
        previous = groups.setdefault(record.query_group_id, category)
        if previous != category:
            raise ValueError("Mixed category labels within a source group.")
        normalized = normalize_text(record.request.description)
        prior = descriptions.setdefault(normalized, record.query_group_id)
        if prior != record.query_group_id:
            raise ValueError("Identical query text in different source groups.")
        by_category[category][record.query_group_id].append(record)
    if len(by_category) < 3:
        raise ValueError("At least three categories needed for supervised training.")
    sets: list[list[LabeledQuery]] = [[], [], []]
    for category in sorted(by_category):
        category_groups = sorted(by_category[category])
        if len(category_groups) < 4:
            raise ValueError(f"Insufficient independent intents for {category}.")
        random.Random(f"{seed}:{category}").shuffle(category_groups)
        assigned = (
            category_groups[:-2],
            category_groups[-2:-1],
            category_groups[-1:],
        )
        for partition, names in zip(sets, assigned, strict=True):
            partition.extend(row for group in names for row in by_category[category][group])
    return CategoryDatasetSplit(
        train=tuple(sets[0]), validation=tuple(sets[1]), test=tuple(sets[2]),
    )


def _text(rows: Sequence[LabeledQuery]) -> list[str]:
    return [normalize_text(record.request.description) for record in rows]


def train_category_classifier(
    training: Sequence[LabeledQuery], *, seed: int = 42,
    regularization_c: float = 1.0,
    feature_mode: Literal["char", "hybrid"] = "char",
) -> Pipeline:
    if not isinstance(regularization_c, (float, int)) or isinstance(regularization_c, bool) or (
        not math.isfinite(regularization_c) or not 0 < regularization_c <= 100
    ):
        raise ValueError("Regularization must be a finite positive C <= 100.")
    if feature_mode not in ("char", "hybrid"):
        raise ValueError("Unsupported category-classifier feature mode.")
    if len(training) < 25:
        raise ValueError("Insufficient category-classifier training examples.")
    labels = [record.request.category for record in training]
    if len(set(labels)) < 3:
        raise ValueError("Insufficient distinct category classes.")
    character_features = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(2, 5),
        lowercase=False, sublinear_tf=True, min_df=1,
    )
    text_features = (
        character_features if feature_mode == "char" else FeatureUnion([
            ("character", cast(TransformerMixin, character_features)),
            ("word", cast(TransformerMixin, TfidfVectorizer(
                analyzer="word", ngram_range=(1, 2),
                token_pattern=r"(?u)\b\w+\b",
                lowercase=False, sublinear_tf=True, min_df=1,
            ))),
        ])
    )
    model = Pipeline([
        ("tfidf", text_features),
        ("classifier", LogisticRegression(
            max_iter=1200, random_state=seed,
            class_weight="balanced", C=float(regularization_c),
        )),
    ])
    model.fit(_text(training), labels)
    return model


def raw_category_scores(pipeline: Pipeline, description: str) -> tuple[str, float, float]:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Request text is missing.")
    if len(description) > 2000:
        raise ValueError("Request text length exceeds typed request contract.")
    probabilities = np.asarray(
        pipeline.predict_proba([normalize_text(description)])[0], dtype=np.float64
    )
    classes = pipeline.named_steps["classifier"].classes_
    if len(classes) < 3 or probabilities.shape != (len(classes),):
        raise ValueError("Category model output shape invalid.")
    if not np.all(np.isfinite(probabilities)) or any(
        value < 0 or value > 1 for value in probabilities
    ):
        raise ValueError("Category probabilities invalid.")
    order = np.argsort(-probabilities, kind="stable")
    first, runner_up = int(order[0]), int(order[1])
    predicted = str(classes[first])
    if predicted not in CATEGORY_ATTRIBUTES:
        raise ValueError("Classifier predicted unknown taxonomy category.")
    return predicted, float(probabilities[first]), float(
        probabilities[first] - probabilities[runner_up]
    )


def calibrate_category_confidence(
    pipeline: Pipeline,
    validation: Sequence[LabeledQuery],
    *,
    min_margin: float = 0.03,
    min_precision: float = 0.95,
) -> tuple[float, dict[str, int | float]]:
    """Validation-only precision constrained abstention; test is never consulted."""
    if not 0 <= min_margin <= 1 or not 0 < min_precision <= 1:
        raise ValueError("Invalid validation guardrail.")
    if not validation:
        raise ValueError("Validation is empty.")
    scored = [
        (record.request.category, *raw_category_scores(pipeline, record.request.description))
        for record in validation
    ]
    thresholds = sorted(
        {0.0, 1.0} | {float(score) for _, _, score, _ in scored}
        | {math.nextafter(score, 1.0) for _, _, score, _ in scored if score < 1}
    )
    eligible: list[tuple[int, int, float, float]] = []
    for threshold in thresholds:
        accepted = [
            label == predicted
            for label, predicted, confidence, margin in scored
            if confidence >= threshold and margin >= min_margin
        ]
        precision = sum(accepted) / len(accepted) if accepted else 1.0
        if precision >= min_precision:
            eligible.append((len(accepted), sum(accepted), precision, threshold))
    if not eligible:
        raise ValueError("Validation cannot support a safe abstention threshold.")
    coverage, correct, precision, threshold = max(
        eligible, key=lambda item: (item[0], item[1], -item[3]),
    )
    return threshold, {
        "validation_queries": len(validation),
        "accepted": coverage,
        "correct_accepted": correct,
        "incorrect_accepted": coverage - correct,
        "abstained": len(validation) - coverage,
        "accepted_precision": precision,
    }


def suggest_category_text(model: CategoryClassifier, description: str) -> CategorySuggestion:
    if not isinstance(model, CategoryClassifier):
        raise TypeError("A verified local category classifier is required.")
    predicted, confidence, margin = raw_category_scores(model.pipeline, description)
    accepted = (
        confidence >= model.threshold and margin >= model.min_margin
        and not description_category_conflict(predicted, description)
    )
    return CategorySuggestion(
        predicted=predicted if accepted else None,
        best_category=predicted,
        confidence=confidence,
        margin=margin,
        threshold=model.threshold,
        abstained=not accepted,
    )


def suggest_category(model: CategoryClassifier, request: RequestedItem) -> CategorySuggestion:
    return suggest_category_text(model, request.description)


def explicitly_use_suggestion(
    request: RequestedItem, suggestion: CategorySuggestion, *,
    accepted_by_caller: bool = False,
) -> RequestedItem:
    """Never silently override an explicit category with an ML prediction."""
    if not accepted_by_caller:
        return request
    if suggestion.predicted is None:
        return request
    if suggestion.predicted not in CATEGORY_ATTRIBUTES:
        raise ValueError("Unrecognized prediction cannot change category.")
    # Validate the entire structured profile again: a previously extracted
    # category-specific attribute cannot be silently carried into another kind.
    return RequestedItem.model_validate({
        **request.model_dump(mode="python"), "category": suggestion.predicted,
    })


def _checked_path(path: Path, *, require_existing: bool) -> Path:
    root = TRUSTED_MODEL_ROOT.resolve()
    location = path.resolve()
    if location.parent != root or location.suffix != ".joblib":
        raise ValueError("Category model must remain in the trusted local model directory.")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Untrusted category model path.")
    if require_existing and not path.is_file():
        raise ValueError("Category model missing.")
    return location


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_category_model(path: Path, model: CategoryClassifier) -> None:
    TRUSTED_MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    target = _checked_path(path, require_existing=False)
    manifest = target.with_suffix(".joblib.manifest.json")
    if target.exists() or manifest.exists():
        raise FileExistsError("Refusing to overwrite a model or its manifest.")
    if not 0 <= model.threshold <= 1 or not 0 <= model.min_margin <= 1:
        raise ValueError("Category confidence guardrail invalid.")
    if len(model.dataset_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in model.dataset_sha256
    ):
        raise ValueError("Dataset integrity identifier invalid.")
    classes = model.pipeline.named_steps["classifier"].classes_
    if any(category not in CATEGORY_ATTRIBUTES for category in classes):
        raise ValueError("Classifier contains unsupported categories.")
    joblib.dump(model.pipeline, target, compress=3)
    metadata: dict[str, Any] = {
        "artifact_format": MODEL_FORMAT,
        "taxonomy_version": TAXONOMY_VERSION,
        "model_sha256": _digest(target),
        "dataset_sha256": model.dataset_sha256,
        "classes": [str(category) for category in classes],
        "split_seed": model.split_seed,
        "threshold": model.threshold,
        "min_margin": model.min_margin,
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "numpy_version": np.__version__,
        "training_data_kind": "synthetic_example",
        "production_eligible": False,
    }
    with manifest.open("x", encoding="utf-8") as output:
        json.dump(metadata, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")


def load_category_model(path: Path) -> CategoryClassifier:
    target = _checked_path(path, require_existing=True)
    manifest = target.with_suffix(".joblib.manifest.json")
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Verified category model manifest missing.")
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Category model manifest invalid.") from error
    if not isinstance(metadata, dict) or (
        metadata.get("artifact_format") != MODEL_FORMAT
        or metadata.get("taxonomy_version") != TAXONOMY_VERSION
        or metadata.get("python_version") != platform.python_version()
        or metadata.get("scikit_learn_version") != sklearn.__version__
        or metadata.get("numpy_version") != np.__version__
        or metadata.get("training_data_kind") != "synthetic_example"
        or metadata.get("production_eligible") is not False
        or metadata.get("model_sha256") != _digest(target)
    ):
        raise ValueError("Category model manifest or SHA256 verification failed.")
    threshold, min_margin = metadata.get("threshold"), metadata.get("min_margin")
    if (
        type(threshold) is not float or not 0 <= threshold <= 1
        or type(min_margin) is not float or not 0 <= min_margin <= 1
    ):
        raise ValueError("Category model confidence metadata invalid.")
    if (
        not isinstance(metadata.get("dataset_sha256"), str)
        or len(metadata["dataset_sha256"]) != 64
        or type(metadata.get("split_seed")) is not int
    ):
        raise ValueError("Category dataset provenance invalid.")
    # Only the local, private, pinned-and-checksummed trusted artifact is unpickled.
    pipeline = joblib.load(target)
    if not isinstance(pipeline, Pipeline):
        raise TypeError("Unexpected local classifier artifact type.")
    classes = pipeline.named_steps["classifier"].classes_
    if [str(category) for category in classes] != metadata.get("classes"):
        raise ValueError("Category classifier labels differ from manifest.")
    return CategoryClassifier(
        pipeline=pipeline, threshold=threshold, min_margin=min_margin,
        dataset_sha256=metadata["dataset_sha256"], split_seed=metadata["split_seed"],
    )
