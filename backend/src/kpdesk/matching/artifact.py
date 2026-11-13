"""Load only self-generated, checksum-verified, versioned local model artifacts.

joblib uses pickle. The configured trusted root must remain private to the
local user; this module does not accept paths supplied through HTTP.
"""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import joblib
import numpy
import sklearn
from sklearn.pipeline import Pipeline

from kpdesk.config import PROJECT_ROOT
from kpdesk.matching.features import FEATURE_SCHEMA

DEFAULT_TRUSTED_ROOT = PROJECT_ROOT / "ml" / "models"


class TrustedArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedModel:
    pipeline: Pipeline
    feature_schema: str
    dataset_sha256: str
    split_seed: int = 42
    training_data_kind: Literal["synthetic_example", "independent_human_reviewed", "manager_reviewed", "unverified"] = "unverified"


def _checked_path(path: Path, trusted_root: Path) -> Path:
    root = trusted_root.resolve()
    resolved = path.resolve()
    if resolved.parent != root or resolved.suffix.casefold() != ".joblib":
        raise TrustedArtifactError("Model path is outside the configured trusted root.")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TrustedArtifactError("Model file must be a regular private file.")
    return resolved


def _manifest_path(path: Path) -> Path:
    return path.with_suffix(".joblib.manifest.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_trusted_artifact(
    path: Path,
    artifact: TrustedModel,
    *,
    trusted_root: Path = DEFAULT_TRUSTED_ROOT,
) -> None:
    """Save a new model under one explicit trusted directory (no overwrites)."""
    trusted_root.mkdir(parents=True, exist_ok=True)
    target = _checked_path(path, trusted_root)
    manifest = _manifest_path(target)
    if target.exists() or manifest.exists():
        raise TrustedArtifactError("Model path already exists; refusing overwrite.")
    if artifact.feature_schema != FEATURE_SCHEMA:
        raise TrustedArtifactError("Unsupported feature schema.")
    if artifact.training_data_kind not in ("synthetic_example", "independent_human_reviewed", "manager_reviewed", "unverified"):
        raise TrustedArtifactError("Unsupported training data provenance.")
    if len(artifact.dataset_sha256) != 64 or any(
        ch not in "0123456789abcdef" for ch in artifact.dataset_sha256.casefold()
    ):
        raise TrustedArtifactError("Invalid dataset SHA256.")

    joblib.dump(artifact.pipeline, target, compress=3)
    content = {
        "feature_schema": artifact.feature_schema,
        "dataset_sha256": artifact.dataset_sha256,
        "split_seed": artifact.split_seed,
        "model_sha256": _sha256(target),
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "numpy_version": numpy.__version__,
        "artifact_format": "joblib-pair-v1",
        "training_data_kind": artifact.training_data_kind,
        "production_eligible": False,
    }
    manifest.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def load_trusted_artifact(
    path: Path,
    expected_schema: str,
    *,
    trusted_root: Path = DEFAULT_TRUSTED_ROOT,
) -> TrustedModel:
    """Check fixed directory, schema, versions and hash *before* joblib deserialization."""
    target = _checked_path(path, trusted_root)
    manifest = _manifest_path(target)
    if not target.is_file() or not manifest.is_file() or manifest.is_symlink():
        raise TrustedArtifactError("Trusted model or manifest is missing.")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise TrustedArtifactError("Trusted model manifest is invalid.") from error
    if not isinstance(data, dict) or data.get("feature_schema") != expected_schema:
        raise TrustedArtifactError("Model feature schema mismatch.")
    if data.get("feature_schema") != FEATURE_SCHEMA or data.get("artifact_format") != "joblib-pair-v1":
        raise TrustedArtifactError("Unsupported model schema or format.")
    if data.get("python_version") != platform.python_version() or (
        data.get("scikit_learn_version") != sklearn.__version__
        or data.get("numpy_version") != numpy.__version__
    ):
        raise TrustedArtifactError("Model library versions differ from training.")
    if not isinstance(data.get("model_sha256"), str) or (
        _sha256(target) != data["model_sha256"]
    ):
        raise TrustedArtifactError("Model SHA256 mismatch; refusing to deserialize.")
    dataset_hash = data.get("dataset_sha256")
    if not isinstance(dataset_hash, str) or len(dataset_hash) != 64:
        raise TrustedArtifactError("Missing dataset SHA256.")
    data_kind = data.get("training_data_kind", "unverified")
    if data_kind not in ("synthetic_example", "independent_human_reviewed", "manager_reviewed", "unverified"):
        raise TrustedArtifactError("Unsupported training data provenance.")
    if data.get("production_eligible", False) is not False:
        raise TrustedArtifactError("Unverified model cannot claim production eligibility.")
    split_seed = data.get("split_seed")
    if not isinstance(split_seed, int) or isinstance(split_seed, bool):
        raise TrustedArtifactError("Invalid training split seed.")
    model = joblib.load(target)
    if not isinstance(model, Pipeline):
        raise TrustedArtifactError("Trusted artifact is not a scikit-learn Pipeline.")
    return TrustedModel(
        pipeline=model,
        feature_schema=data["feature_schema"],
        dataset_sha256=dataset_hash,
        split_seed=split_seed,
        training_data_kind=data_kind,
    )
