"""Session-bound durable feedback; immutable records, explicit consent withdrawal."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from kpdesk.db.authenticated_tenant import bind_authenticated_tenant
from kpdesk.matching.feedback_bundle import FeedbackBundle, FeedbackExportRequest
from kpdesk.quotes.storage import QuoteConflict, QuoteNotFound


class FeedbackSummary(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    feedback_id: UUID
    quote_id: UUID
    revision: int = Field(ge=1)
    reviewer_id: UUID
    created_at: datetime
    withdrawn_at: datetime | None
    event_count: int = Field(ge=1, le=420)
    curation_status: Literal['pending_review'] = 'pending_review'


class FeedbackHistory(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    items: tuple[FeedbackSummary, ...] = Field(max_length=20)
    next_cursor: UUID | None


def decision_digest(body: FeedbackExportRequest) -> str:
    value = body.model_dump(mode='json')
    value['decisions'] = sorted(value['decisions'], key=lambda row: row['line_number'])
    for row in value['decisions']:
        row['rejected_skus'] = sorted(row['rejected_skus'])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class TrainingFeedbackStore:
    connection: Connection
    organization_id: UUID
    cookie: str
    csrf: str | None = None

    def _authorize(self) -> None:
        bind_authenticated_tenant(self.connection, session_token=self.cookie,
                                  organization_id=self.organization_id, operation='manage', csrf=self.csrf)

    def _call(self, statement: str, values: dict[str, object], *, bundle: bool = False, optional: bool = False) -> object:
        self._authorize()
        query = text(statement)
        if bundle:
            query = query.bindparams(bindparam('bundle', type_=JSONB))
        result: object = self.connection.scalar(query, {'org': self.organization_id, **values})
        self._authorize()
        if result is None and not optional:
            raise QuoteNotFound('feedback_not_found')
        if isinstance(result, dict) and 'error' in result:
            raise QuoteConflict(str(result['error']))
        return result

    def retry(self, quote_id: UUID, body: FeedbackExportRequest, key: str) -> FeedbackSummary | None:
        result = self._call(
            'SELECT public.kpdesk_training_feedback_retry(:org,:quote,:revision,:sha,:digest,:key)',
            {'quote': quote_id, 'revision': body.expected_revision, 'sha': body.source_snapshot_sha256,
             'digest': decision_digest(body), 'key': key}, optional=True)
        return None if result is None else FeedbackSummary.model_validate(result)

    def save(self, bundle: FeedbackBundle, body: FeedbackExportRequest, key: str) -> FeedbackSummary:
        if bundle.organization_id != self.organization_id:
            raise ValueError('feedback_bundle_scope_mismatch')
        return FeedbackSummary.model_validate(self._call(
            'SELECT public.kpdesk_training_feedback_save(:org,:bundle,:digest,:key)',
            {'bundle': bundle.model_dump(mode='json'), 'digest': decision_digest(body), 'key': key}, bundle=True))

    def history(self, quote_id: UUID, before: UUID | None = None) -> FeedbackHistory:
        return FeedbackHistory.model_validate(self._call(
            'SELECT public.kpdesk_training_feedback_history(:org,:quote,:before)',
            {'quote': quote_id, 'before': before}))

    def get(self, quote_id: UUID, feedback_id: UUID) -> FeedbackBundle:
        return FeedbackBundle.model_validate(self._call(
            'SELECT public.kpdesk_training_feedback_get(:org,:quote,:id)',
            {'quote': quote_id, 'id': feedback_id}))

    def withdraw(self, quote_id: UUID, feedback_id: UUID) -> FeedbackSummary:
        return FeedbackSummary.model_validate(self._call(
            'SELECT public.kpdesk_training_feedback_withdraw(:org,:quote,:id)',
            {'quote': quote_id, 'id': feedback_id}))
