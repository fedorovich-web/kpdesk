"""Map only validated extracted facts into manager-reviewed matching requests."""

from __future__ import annotations

from kpdesk.extraction.schema import ExtractedLine
from kpdesk.matching.schema import RequestedItem


class ExtractionNeedsReview(ValueError):
    """Unknown category cannot enter deterministic product matching."""


def to_requested_item(line: ExtractedLine) -> RequestedItem:
    if line.category is None:
        raise ExtractionNeedsReview("category needs manual classification")
    return RequestedItem.model_validate({
        "description": line.description,
        "category": line.category,
        "quantity": line.quantity,
        "unit": line.unit,
        "brand": line.brand,
        "paper_format": line.paper_format,
        "paper_gsm": line.paper_gsm,
        "pen_ink_color": line.pen_ink_color,
        "pen_ink_type": line.pen_ink_type,
        "attributes": {key: value for key, value in line.attributes.items() if value is not None},
    })
