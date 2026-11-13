"""Evidence excerpts must actually exist in each immutable source document."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook
from reportlab.pdfgen import canvas

from kpdesk.matching.real_requests import load_request_intake
from kpdesk.matching.source_evidence import locate_source_excerpt


def _source(tmp_path: Path, filename: str, content: bytes, phrase: str) -> Path:
    directory = tmp_path / "sources"
    directory.mkdir(exist_ok=True)
    (directory / filename).write_bytes(content)
    intake = {
        "schema_version": "real-request-intake-v1",
        "records": [{
            "request_id": "q1", "origin_declared": "public_procurement",
            "source_batch_id": "batch1", "source_file": filename,
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "source_quote": phrase, "request_text": phrase,
            "captured_at": "2026-10-09T12:00:00+02:00",
            "rights_status": "review_required", "privacy_status": "review_required",
        }],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(intake, ensure_ascii=False), encoding="utf-8")
    return path


def test_text_quote_verified_with_exact_document_occurrence(tmp_path: Path) -> None:
    phrase = "Бумага А4, плотность 80 г/м2, 20 упаковок"
    path = _source(tmp_path, "request.txt", ("Заявка:\n" + phrase).encode(), phrase)
    verified = load_request_intake(path, tmp_path / "sources")
    assert verified.source_excerpts_verified is True
    assert verified.independently_verified is False
    assert locate_source_excerpt(tmp_path / "sources/request.txt", phrase) == "text:1"


def test_text_not_in_original_cannot_pass_just_by_matching_sha(tmp_path: Path) -> None:
    phrase = "Бумага А4, плотность 80 г/м2, 20 упаковок"
    path = _source(tmp_path, "request.txt", "Заявка на скрепки".encode(), phrase)
    with pytest.raises(ValueError, match="excerpt|source"):
        load_request_intake(path, tmp_path / "sources")


def test_rewritten_quantities_do_not_count_as_source_evidence(tmp_path: Path) -> None:
    phrase = "Нужно 10 коробок с бумажными папками А4"
    path = _source(tmp_path, "request.txt", phrase.encode(), phrase)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["records"][0]["request_text"] = "Нужно 100 коробок с бумажными папками А4"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="request_text|source_quote|verbatim"):
        load_request_intake(path, tmp_path / "sources")


def test_csv_cp1251_source_quote_supported(tmp_path: Path) -> None:
    phrase = "Маркеры перманентные синие 12 штук"
    path = _source(tmp_path, "request.csv", ("Артикул;Описание\n1;" + phrase).encode("cp1251"), phrase)
    assert load_request_intake(path, tmp_path / "sources").source_excerpts_verified


def test_xlsx_quote_is_located_in_cell_not_formula(tmp_path: Path) -> None:
    phrase = "Тонер для лазерного принтера 6 штук"
    book = Workbook()
    page = book.active
    assert page is not None
    page["B3"] = phrase
    buffer = io.BytesIO()
    book.save(buffer)
    path = _source(tmp_path, "request.xlsx", buffer.getvalue(), phrase)
    assert locate_source_excerpt(tmp_path / "sources/request.xlsx", phrase) == "sheet:Sheet!B3"
    assert load_request_intake(path, tmp_path / "sources").source_excerpts_verified


def test_docx_phrase_in_paragraphs(tmp_path: Path) -> None:
    phrase = "Скрепки оцинкованные длиной 50 мм"
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>' + phrase + '</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
    path = _source(tmp_path, "request.docx", buf.getvalue(), phrase)
    assert load_request_intake(path, tmp_path / "sources").source_excerpts_verified


def test_pdf_source_quote_located_on_page(tmp_path: Path) -> None:
    phrase = "Paper A4 80 gsm 12 boxes"
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(70, 740, phrase)
    pdf.showPage()
    pdf.save()
    path = _source(tmp_path, "request.pdf", buf.getvalue(), phrase)
    assert locate_source_excerpt(tmp_path / "sources/request.pdf", phrase) == "page:1"
    assert load_request_intake(path, tmp_path / "sources").source_excerpts_verified


def test_ambiguous_duplicate_excerpt_requires_manual_disambiguation(tmp_path: Path) -> None:
    phrase = "Бумага А4 для принтера 20 пачек"
    path = _source(tmp_path, "request.txt", (phrase + "\n" + phrase).encode(), phrase)
    with pytest.raises(ValueError, match="ambiguous|multiple"):
        load_request_intake(path, tmp_path / "sources")


def test_unsafe_zip_expansion_is_rejected(tmp_path: Path) -> None:
    phrase = "Папки архивные на кольцах 20 штук"
    buf = io.BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 72_000_000)
        archive.writestr("[Content_Types].xml", "<Types/>")
    path = _source(tmp_path, "request.docx", buf.getvalue(), phrase)
    with pytest.raises(ValueError, match="ZIP|size|excessive"):
        load_request_intake(path, tmp_path / "sources")


def test_unknown_source_evidence_not_accepted(tmp_path: Path) -> None:
    phrase = "Файлы для бумаг формата А4 500 штук"
    path = _source(tmp_path, "request.txt", phrase.encode(), phrase)
    body = json.loads(path.read_text(encoding="utf-8"))
    del body["records"][0]["source_quote"]
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="source_quote|source record"):
        load_request_intake(path, tmp_path / "sources")


def test_corrupt_pdf_and_image_only_pdf_fail_closed(tmp_path: Path) -> None:
    phrase = "Paper A4 80 gsm 12 boxes"
    path = _source(tmp_path, "request.pdf", b"%PDF-1.7\nnot a PDF document", phrase)
    with pytest.raises(ValueError, match="PDF|source"):
        load_request_intake(path, tmp_path / "sources")

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.showPage()
    document.save()
    path = _source(tmp_path, "request.pdf", buffer.getvalue(), phrase)
    with pytest.raises(ValueError, match="excerpt|source"):
        load_request_intake(path, tmp_path / "sources")


def test_docx_macro_entry_rejected_even_with_matching_text(tmp_path: Path) -> None:
    phrase = "Бумага для записей пять пачек А4"
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body><w:p><w:r><w:t>' + phrase + '</w:t></w:r></w:p></w:body></w:document>')
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/vbaProject.bin", b"unsafe macro content")
    path = _source(tmp_path, "request.docx", buffer.getvalue(), phrase)
    with pytest.raises(ValueError, match="macro|ZIP|structure"):
        load_request_intake(path, tmp_path / "sources")
