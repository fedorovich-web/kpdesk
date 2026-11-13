"""Automatic draft endpoint: user provides extracted facts, server selects and prices."""
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from test_catalog_api import FakeCatalogRepository

from kpdesk.api.routes import get_catalog_repository
from kpdesk.catalog.storage import PublishedCatalog
from kpdesk.main import create_app
from kpdesk.matching.schema import CatalogProduct


def extracted(category: str | None, description: str, attrs: dict[str, object], count: int = 2) -> dict[str, object]:
    return {
        "source_quote": description,
        "description": description,
        "category": category,
        "quantity": count,
        "unit": "штука",
        "brand": None,
        "paper_format": None,
        "paper_gsm": None,
        "pen_ink_color": None,
        "pen_ink_type": None,
        "attributes": attrs,
    }


def make_client() -> tuple[TestClient, str]:
    version=PublishedCatalog(id=uuid.uuid4(),source_sha256="b"*64,rows_count=2)
    repository=FakeCatalogRepository(
        published=version,
        products=(
            CatalogProduct(
                sku="PEN-1",name="Карандаш графитовый HB",category="pencil",
                sale_unit="штука",pieces_per_sale_unit=1,price_rub=Decimal("19.90"),
                attributes={"pencil_type":"graphite","lead_grade":"HB"},
            ),
            CatalogProduct(
                sku="FOLDER-1",name="Папка для документов A4 на молнии",
                category="document_folder",sale_unit="штука",
                pieces_per_sale_unit=1,price_rub=Decimal("125.50"),
                attributes={"folder_kind":"zip","format":"A4"},
            ),
        ),
    )
    app=create_app(testing=True)
    app.dependency_overrides[get_catalog_repository]=lambda:repository
    return TestClient(app),str(version.id)


def test_full_extracted_request_yields_automatic_draft_without_approval() -> None:
    client,version=make_client()
    payload={
        "catalog_version_id":version,
        "extracted_request":{"lines":[
            extracted("pencil","Карандаш графитовый HB",{"pencil_type":"graphite","lead_grade":"HB"},10),
            extracted("document_folder","Папка для документов A4 на молнии",{"folder_kind":"zip","format":"A4"},2),
        ]},
    }
    response=client.post("/api/quotes/auto-draft",json=payload,headers={"Origin":"http://localhost:5173"})
    assert response.status_code==200,response.text
    body=response.json()
    assert body["ready_for_draft"] is True
    assert body["total_rub"]=="450.00"
    assert [line["status"] for line in body["lines"]]==["auto_selected","auto_selected"]
    assert body["approval_status"]=="not_approved"
    assert body["catalog_version_id"]==version


def test_unknown_category_is_exception_line_not_whole_request_failure() -> None:
    client,version=make_client()
    result=client.post(
        "/api/quotes/auto-draft",
        json={"catalog_version_id":version,"extracted_request":{"lines":[
            extracted("pencil","Карандаш графитовый HB",{"pencil_type":"graphite","lead_grade":"HB"},10),
            extracted(None,"Неизвестный расходник",{},1),
        ]}},
        headers={"Origin":"http://localhost:5173"},
    )
    assert result.status_code==200,result.text
    body=result.json()
    assert body["ready_for_draft"] is False
    assert body["total_rub"] is None
    assert [line["status"] for line in body["lines"]]==["auto_selected","needs_review"]
    assert body["lines"][1]["reason_codes"]==["category_needs_review"]


def test_stale_catalog_version_rejected() -> None:
    client,_=make_client()
    result=client.post(
        "/api/quotes/auto-draft",
        json={"catalog_version_id":str(uuid.uuid4()),"extracted_request":{"lines":[
            extracted("pencil","Карандаш графитовый HB",{"pencil_type":"graphite","lead_grade":"HB"},1),
        ]}},
        headers={"Origin":"http://localhost:5173"},
    )
    assert result.status_code==409
    assert result.json()["code"]=="stale_catalog"
