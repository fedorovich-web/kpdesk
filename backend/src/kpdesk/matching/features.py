"""Train-time and inference-time features derived from the same pair contract."""

from __future__ import annotations

from dataclasses import astuple, dataclass
from typing import Self

import numpy as np
from numpy.typing import NDArray
from rapidfuzz import fuzz
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.utils.validation import check_is_fitted

from kpdesk.matching.attribute_values import normalize_attribute_value
from kpdesk.matching.constraints import check_constraints
from kpdesk.matching.normalize import normalize_text
from kpdesk.matching.profiles import CATEGORY_ATTRIBUTES, REQUIRED_PRODUCT_ATTRIBUTES
from kpdesk.matching.schema import CatalogProduct, RequestedItem

FEATURE_SCHEMA = "kpdesk-pair-v3"


@dataclass(frozen=True, slots=True)
class PairInput:
    request: RequestedItem
    product: CatalogProduct


@dataclass(frozen=True, slots=True)
class PairFeatures:
    weighted_fuzzy: float
    token_set_fuzzy: float
    same_category: float
    brand_exact: float
    format_exact: float
    gsm_exact: float
    ink_color_exact: float
    ink_type_exact: float
    sale_unit_exact: float
    compatible: float
    needs_review: float
    profile_exact_match_fraction: float
    request_profile_coverage: float


def _equal_optional(want: str | int | None, actual: str | int | None, field: str = '') -> float:
    if want is None or actual is None:
        return 0.0
    return float(normalize_attribute_value(field, want) == normalize_attribute_value(field, actual))


def build_pair_features(request: RequestedItem, product: CatalogProduct) -> PairFeatures:
    """Never include price, SKU or gold labels as a predictor."""
    req_text = normalize_text(request.description)
    prod_text = normalize_text(product.name)
    constraint = check_constraints(request, product)
    policy = CATEGORY_ATTRIBUTES[request.category]
    requested = [rule for rule in policy if rule.name in request.attributes]
    matches = 0
    for rule in requested:
        expected = request.attributes[rule.name]
        actual = product.attributes.get(rule.name)
        if actual is None:
            continue
        if rule.mode == "minimum":
            matches += int(isinstance(actual, int) and isinstance(expected, int) and actual >= expected)
        else:
            matches += int(normalize_attribute_value(rule.name, str(actual)) == normalize_attribute_value(rule.name, str(expected)))
    required = REQUIRED_PRODUCT_ATTRIBUTES[request.category]
    coverage = (
        len(required.intersection(request.attributes)) / len(required)
        if required else 0.0
    )
    return PairFeatures(
        weighted_fuzzy=fuzz.WRatio(req_text, prod_text) / 100.0,
        token_set_fuzzy=fuzz.token_set_ratio(req_text, prod_text) / 100.0,
        same_category=float(request.category == product.category),
        brand_exact=_equal_optional(request.brand, product.brand),
        format_exact=_equal_optional(request.paper_format, product.paper_format),
        gsm_exact=_equal_optional(request.paper_gsm, product.paper_gsm),
        ink_color_exact=_equal_optional(request.pen_ink_color, product.pen_ink_color, 'pen_ink_color'),
        ink_type_exact=_equal_optional(request.pen_ink_type, product.pen_ink_type, 'pen_ink_type'),
        sale_unit_exact=_equal_optional(request.unit, product.sale_unit),
        compatible=float(constraint.status == "compatible"),
        needs_review=float(constraint.status == "needs_review"),
        profile_exact_match_fraction=matches / len(requested) if requested else 0.0,
        request_profile_coverage=coverage,
    )


class PairFeatureExtractor(TransformerMixin, BaseEstimator):
    """Train-only fitted char vocabulary; transformed rows are numeric pair similarities."""

    def __init__(self) -> None:
        self.vectorizer_: TfidfVectorizer

    def fit(self, x: list[PairInput], y: object = None) -> Self:
        if not x:
            raise ValueError("Training pairs are empty.")
        texts = [
            normalize_text(text)
            for pair in x
            for text in (pair.request.description, pair.product.name)
        ]
        self.vectorizer_ = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(2, 4), lowercase=False, dtype=np.float64,
        )
        self.vectorizer_.fit(texts)
        return self

    def transform(self, x: list[PairInput]) -> NDArray[np.float64]:
        check_is_fitted(self, "vectorizer_")
        if not x:
            return np.empty((0, 14), dtype=np.float64)
        queries = self.vectorizer_.transform(
            [normalize_text(pair.request.description) for pair in x]
        )
        names = self.vectorizer_.transform(
            [normalize_text(pair.product.name) for pair in x]
        )
        tfidf_cosines = np.asarray(queries.multiply(names).sum(axis=1)).reshape(-1)
        scalars = [
            astuple(build_pair_features(pair.request, pair.product))
            for pair in x
        ]
        return np.column_stack((tfidf_cosines, np.array(scalars, dtype=np.float64)))
