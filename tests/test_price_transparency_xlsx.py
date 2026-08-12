import os
import shutil

import pytest
from sqlalchemy import func, select

from hospitals import price_transparency as pt
from hospitals.db import charge_sources, make_engine, standard_charges
from hospitals.ingest_charges import ingest_charge_file, ingest_charge_path

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
XLSX = os.path.join(FIX, "mrf_tall_sample.xlsx")
CSV = os.path.join(FIX, "mrf_tall_sample.csv")


def test_xlsx_and_csv_parse_identically():
    """A spreadsheet MRF is just another way of shipping the same rows."""

    meta_x, rows_x = pt.read_any(XLSX)
    meta_c, rows_c = pt.read_any(CSV)
    rows_x, rows_c = list(rows_x), list(rows_c)

    assert meta_x.layout == meta_c.layout == "tall"
    assert meta_x.hospital_name == meta_c.hospital_name
    assert meta_x.primary_npi == meta_c.primary_npi
    assert meta_x.license_number == meta_c.license_number
    assert meta_x.license_state == meta_c.license_state
    assert meta_x.last_updated_on == meta_c.last_updated_on

    assert len(rows_x) == len(rows_c) == 3
    for a, b in zip(rows_x, rows_c):
        assert a == b


def test_read_any_dispatches_xlsx():
    meta, rows = pt.read_any(XLSX)
    assert meta.layout == "tall"          # not routed to the JSON reader
    assert meta.source_file.endswith(".xlsx")
    assert len(list(rows)) == 3


def test_xlsx_limit_caps_items():
    _, rows = pt.read_any(XLSX, limit=1)
    assert len(list(rows)) == 1


def test_ingest_xlsx_end_to_end(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'x.sqlite'}"
    summary = ingest_charge_file(XLSX, database_url=db_url)
    assert summary.layout == "tall"
    assert summary.charges_loaded == 3

    engine = make_engine(db_url)
    with engine.connect() as conn:
        src = conn.execute(select(charge_sources)).mappings().one()
        assert src["hospital_name"] == "Sunflower General Hospital"
        n = conn.execute(select(func.count()).select_from(standard_charges)).scalar_one()
    assert n == 3


def test_directory_mode_picks_up_xlsx(tmp_path):
    """A mixed download folder ingests in one pass."""

    src_dir = tmp_path / "round"
    src_dir.mkdir()
    shutil.copy(XLSX, src_dir / "111111111_a_standardcharges.xlsx")
    shutil.copy(CSV, src_dir / "222222222_b_standardcharges.csv")
    shutil.copy(os.path.join(FIX, "mrf_sample.json"), src_dir / "333333333_c_standardcharges.json")

    db_url = f"sqlite:///{tmp_path / 'dir.sqlite'}"
    summaries = ingest_charge_path(str(src_dir), database_url=db_url)

    assert len(summaries) == 3
    assert {s.ein for s in summaries} == {"111111111", "222222222", "333333333"}
    assert {s.layout for s in summaries} == {"tall", "json"}
    assert sum(s.charges_loaded for s in summaries) == 9  # 3 + 3 + 3


def test_missing_openpyxl_is_reported_clearly(monkeypatch):
    """The error should name the fix, not surface an ImportError."""

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **k):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ValueError, match="openpyxl"):
        pt.read_any(XLSX)
