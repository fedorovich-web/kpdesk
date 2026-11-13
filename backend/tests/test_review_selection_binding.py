"""A reviewed extraction must bind the same full payload used by quote creation."""
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import Connection
from test_document_alternatives import saved

from kpdesk.quotes.instructions import ConfirmedInstructions, QuoteRules
from kpdesk.quotes.line import ManagerSkuSelection
from kpdesk.quotes.review_audit import ReviewEvidence, attest_quote_review
from kpdesk.quotes.storage import _hash_request


def test_review_digest_includes_explicit_manager_selection() -> None:
    quote,_=saved();extracted=quote.source_request
    assert extracted is not None
    connection=Mock(spec=Connection);connection.scalar.return_value=True
    selections=[ManagerSkuSelection(line_number=1,sku='FIRST',approved_sale_quantity=20)]
    evidence=ReviewEvidence(extraction_id=uuid4(),source_text=extracted.lines[0].source_quote,
                            original_extracted_request=extracted,reviewed_lines=[True])
    attest_quote_review(cast(Connection,connection),organization_id=uuid4(),quote_id=quote.quote_id,
                       revision=1,catalog_id=quote.catalog_version_id,extracted=extracted,
                       expected_revision=None,evidence=evidence,selections=selections)
    assert connection.scalar.call_args.args[1]['request']==_hash_request(quote.catalog_version_id,extracted,None,None,selections)


def test_revised_review_digest_includes_confirmed_rules_and_source_revision() -> None:
    quote,_=saved();extracted=quote.source_request
    assert extracted is not None
    connection=Mock(spec=Connection);connection.scalar.return_value=True
    instructions=ConfirmedInstructions(raw_text='Сохраняй обязательные характеристики',rules=QuoteRules())
    selections=[ManagerSkuSelection(line_number=1,sku='FIRST')]
    evidence=ReviewEvidence(extraction_id=uuid4(),source_text=extracted.lines[0].source_quote,
                            original_extracted_request=extracted,reviewed_lines=[True])
    attest_quote_review(cast(Connection,connection),organization_id=uuid4(),quote_id=quote.quote_id,
                       revision=2,catalog_id=quote.catalog_version_id,extracted=extracted,
                       expected_revision=1,evidence=evidence,selections=selections,instructions=instructions)
    assert connection.scalar.call_args.args[1]['request']==_hash_request(
        quote.catalog_version_id,extracted,quote.quote_id,1,selections,instructions)
