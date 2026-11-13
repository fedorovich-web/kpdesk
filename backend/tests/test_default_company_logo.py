from io import BytesIO
from typing import cast

import pytest
from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.generic import DictionaryObject
from test_company_logo import source_logo
from test_pdf_brand_v2 import branded_proposal

from kpdesk.company.logo import prepare_logo
from kpdesk.company.schema import CompanyProfileSnapshot, profile_digest
from kpdesk.quotes.appearance import DocumentAppearance
from kpdesk.quotes.document import CommercialDocument
from kpdesk.quotes.pdf import render_document_preview


def images(raw: bytes) -> list[tuple[int, int]]:
    resources = cast(DictionaryObject, PdfReader(BytesIO(raw)).pages[0]["/Resources"].get_object())
    objects = cast(DictionaryObject, resources.get("/XObject", DictionaryObject()).get_object())
    return [(cast(int, value["/Width"]), cast(int, value["/Height"])) for ref in objects.values()
            if (value := cast(DictionaryObject, ref.get_object())).get("/Subtype") == "/Image"]


@pytest.mark.parametrize("without_profile", [False, True])
def test_default_kpdesk_logo_is_in_pdf_when_company_has_not_uploaded_one(without_profile: bool) -> None:
    quote, document = branded_proposal()
    assert document.appearance is not None
    appearance = DocumentAppearance.model_validate({**document.appearance.model_dump(), "default_logo": "kpdesk_v1"})
    document = document.model_copy(update={"appearance": appearance,
                                          "company_profile": None if without_profile else document.company_profile})
    assert (203, 52) in images(render_document_preview(quote, document))
    assert CommercialDocument.model_validate_json(document.model_dump_json()).appearance == appearance


def test_uploaded_company_logo_replaces_kpdesk_default_completely() -> None:
    quote, document = branded_proposal()
    assert document.appearance is not None and document.company_profile is not None
    data = document.company_profile.data.model_copy(update={"logo": prepare_logo(source_logo())})
    snapshot = CompanyProfileSnapshot(revision=2, data=data, sha256=profile_digest(data))
    appearance = DocumentAppearance.model_validate({**document.appearance.model_dump(), "default_logo": "kpdesk_v1"})
    raw = render_document_preview(quote, document.model_copy(update={"appearance": appearance, "company_profile": snapshot}))
    sizes = images(raw)
    assert (400, 120) in sizes and (203, 52) not in sizes


def test_old_appearance_has_no_added_default_logo_key_and_external_logos_are_forbidden() -> None:
    assert "default_logo" not in DocumentAppearance().model_dump(mode="json")
    for value in ("https://example.invalid/logo.png", "file:///private", "unknown", True):
        with pytest.raises(ValidationError):
            DocumentAppearance.model_validate({"default_logo": value})
