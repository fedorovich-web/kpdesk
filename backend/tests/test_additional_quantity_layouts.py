"""Different buyer spreadsheets have different quantity/unit evidence columns."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kpdesk.matching.source_evidence import extract_source_spans, extract_xlsx_row_amount

ROOT=Path(__file__).resolve().parents[2]/"ml/data/real/public_procurement_requests"


def _excerpt(filename:str, cell:str)->tuple[Path,str,str]:
    path=ROOT/filename
    raw=path.read_bytes()
    loc,text=next((p,v) for p,v in extract_source_spans(path,raw) if p.endswith("!"+cell))
    assert loc.endswith("!"+cell)
    return path,text,hashlib.sha256(raw).hexdigest()


def test_design_procurement_has_quantity_in_d_and_no_unit() -> None:
    path,text,sha=_excerpt("bidzaar_337279_design_2026.xlsx","C4")
    result=extract_xlsx_row_amount(path,text,expected_sha256=sha,title_column="C",
                                   quantity_column="D",unit_column=None)
    assert result["quantity"] == 30
    assert isinstance(result["quantity_cell"], str)
    assert result["quantity_cell"].endswith("!D4")
    assert result["unit_original"] is None
    assert result["unit_cell"] is None


def test_tmc_procurement_formula_derived_annual_qty_is_not_promoted() -> None:
    path,text,sha=_excerpt("bidzaar_364642_tmc_2026.xlsx","B12")
    with pytest.raises(ValueError, match="Formula-derived"):
        extract_xlsx_row_amount(path,text,expected_sha256=sha,title_column="B",
                                quantity_column="E",unit_column="D")
