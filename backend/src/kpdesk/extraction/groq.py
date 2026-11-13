"""Deprecated import facade. Active transport is exclusively OpenRouter.

Names are retained for stored tooling imports; there is no Groq HTTP transport.
"""
from kpdesk.extraction.service import (
    _parse_category_request,
    _structured_schema,
    _unique_json_object,
    extract_request,
)
from kpdesk.llm.client import URL as GROQ_URL
from kpdesk.llm.client import LLMError as GroqExtractionError
from kpdesk.llm.client import structured_inference
from kpdesk.llm.settings import MODEL as DEFAULT_MODEL
from kpdesk.llm.settings import LLMSettings as GroqSettings

ALLOWED_MODELS = frozenset((DEFAULT_MODEL,))
__all__ = [
    "ALLOWED_MODELS",
    "DEFAULT_MODEL",
    "GROQ_URL",
    "GroqExtractionError",
    "GroqSettings",
    "_parse_category_request",
    "_structured_schema",
    "_unique_json_object",
    "extract_request",
    "structured_inference",
]
