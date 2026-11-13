from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.artifact import TrustedModel, load_trusted_artifact
from kpdesk.matching.category_classifier import CategoryClassifier, load_category_model
from kpdesk.matching.features import FEATURE_SCHEMA
from kpdesk.matching.profiles import TAXONOMY_VERSION

MODEL_ROOT = PROJECT_ROOT / "ml" / "models"
RERANKER_NAME = "taxonomy-a39-20261022.joblib"
RERANKER_SHA256 = "fd6dc363e0fb2bdc691c18b88db9f7419a226b647768623dffc54e771bf2aece"
CATEGORY_NAME = "category-a39-20261022.joblib"
CATEGORY_SHA256 = "a5631d4d9ae2b1f516953edc52e5996d35267417b6afdc683cb1b246c3a492e7"


@dataclass(frozen=True, slots=True)
class LocalMLModels:
    reranker: TrustedModel
    classifier: CategoryClassifier


def _assert_pinned(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("Pinned model SHA256 mismatch.")


@lru_cache(maxsize=1)
def load_local_ml_models() -> LocalMLModels:
    if TAXONOMY_VERSION != "kpdesk-taxonomy-v4":
        raise ValueError("Synthetic model taxonomy mismatch.")
    reranker_path = MODEL_ROOT / RERANKER_NAME
    classifier_path = MODEL_ROOT / CATEGORY_NAME
    _assert_pinned(reranker_path, RERANKER_SHA256)
    _assert_pinned(classifier_path, CATEGORY_SHA256)
    reranker = load_trusted_artifact(reranker_path, FEATURE_SCHEMA)
    classifier = load_category_model(classifier_path)
    if reranker.training_data_kind != "synthetic_example":
        raise ValueError("Unexpected model training provenance.")
    return LocalMLModels(reranker=reranker, classifier=classifier)
