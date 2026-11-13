from io import BytesIO
from typing import cast

import pymupdf
import pytest
from pypdf import PdfReader
from pypdf.generic import DictionaryObject
from test_company_logo import source_logo
from test_pdf_brand_v2 import branded_proposal

from kpdesk.company.logo import prepare_logo
from kpdesk.company.schema import CompanyProfileSnapshot, profile_digest
from kpdesk.quotes.narrative import proposal_facts
from kpdesk.quotes.pdf import render_document_preview


@pytest.mark.parametrize("size", [(800, 160), (200, 200), (120, 400)])
def test_logo_is_embedded_in_header_at_original_aspect_without_clipping(size: tuple[int, int]) -> None:
    quote, document = branded_proposal()
    assert document.company_profile is not None
    logo = prepare_logo(source_logo(size))
    data = document.company_profile.data.model_copy(update={"logo": logo})
    snapshot = CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))
    document = document.model_copy(update={"company_profile": snapshot})
    raw = render_document_preview(quote, document)
    reader = PdfReader(BytesIO(raw))
    resources = cast(DictionaryObject, reader.pages[0]["/Resources"].get_object())
    objects = cast(DictionaryObject, resources.get("/XObject", DictionaryObject()).get_object())
    images = [cast(DictionaryObject, item.get_object()) for item in objects.values()]
    assert any(image.get("/Width") == logo.width and image.get("/Height") == logo.height for image in images)
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:  # type: ignore[no-untyped-call]
        page = pdf[0]
        image_info = page.get_images(full=True)
        assert image_info
        rect = page.get_image_rects(image_info[0][0])[0]
        assert rect.width <= 96.01 and rect.height <= 40.01
        assert abs(rect.width / rect.height - logo.width / logo.height) < .001
        assert rect.x0 >= 48 and rect.y0 >= 50 and rect.y1 < 145
        issuer = min(page.search_for(document.issuer_name), key=lambda item: item.y0)
        assert rect.x1 + 7 <= issuer.x0
    assert render_document_preview(quote, document) == raw


def test_logo_data_and_digest_never_enter_llm_facts() -> None:
    quote, document = branded_proposal()
    assert document.company_profile is not None
    logo = prepare_logo(source_logo())
    data = document.company_profile.data.model_copy(update={"logo": logo})
    snapshot = CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))
    facts = proposal_facts(quote, issuer_name=document.issuer_name, recipient_name=document.recipient_name,
                           manager_brief=document.manager_brief, commercial_terms=document.commercial_terms,
                           company_profile=snapshot)
    assert logo.data_base64 not in facts and logo.sha256 not in facts
    assert '"logo"' not in facts and "data_base64" not in facts
