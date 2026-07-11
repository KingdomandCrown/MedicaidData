import importlib.util
import os

import pytest

openpyxl = pytest.importorskip("openpyxl")

# Standalone script, loaded by file path like the resolver in
# test_resolve_websites.py.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "xlsx_to_resolver_input.py")
_spec = importlib.util.spec_from_file_location("xlsx_to_resolver_input", SCRIPT)
conv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conv)


def _make_workbook(path):
    """Two sheets mimicking the AHD-style export: preamble rows of varying
    length, then a header row found by its 'Hospital Name' cell."""
    wb = openpyxl.Workbook()

    ks = wb.active
    ks.title = "Kansas"
    ks.append(["Table of Search Results - July 10, 2026"])
    ks.append([])
    ks.append(["Search Subject", "Criteria Selected"])
    ks.append(["State", "KS"])
    ks.append([])
    ks.append(["Hospital Name", "CMS Certification Number", "Beds", "City", "State", "ZIP", "Telephone"])
    ks.append(["Sunflower General Hospital", "170012", 220, "Wichita", "KS", 67214, "(316) 555-0100"])
    ks.append(["Fort Riley Army Hospital", "17013f", 0, "Fort Riley", "KS", 66442, "(785) 555-0101"])
    ks.append(["Satellite Campus (no CCN)", None, 0, "Topeka", "KS", 66604, "(785) 555-0102"])
    ks.append([])  # trailing blank row

    ct = wb.create_sheet("Connecticut")
    ct.append(["Table of Search Results - July 10, 2026"])
    ct.append([])
    ct.append(["Hospital Name", "CMS Certification Number", "Beds", "City", "State", "ZIP", "Telephone"])
    ct.append(["Nutmeg Medical Center", 70001, 150, "Hartford", "CT", 6103, "(860) 555-0103"])
    ct.append(["Sunflower General Hospital", "170012", 220, "Wichita", "KS", 67214, "(316) 555-0100"])  # dup CCN

    empty = wb.create_sheet("NoTable")
    empty.append(["Nothing to see here"])

    wb.save(path)


def test_convert_end_to_end(tmp_path):
    xlsx = tmp_path / "hospitals.xlsx"
    _make_workbook(xlsx)
    out = tmp_path / "input.csv"
    sidecar = tmp_path / "input_no_ccn.csv"

    stats = conv.convert(str(xlsx), str(out), str(sidecar))

    assert stats["with_ccn"] == 3
    assert stats["no_ccn"] == 1
    assert stats["duplicates"] == 1
    assert stats["sheets_missing_table"] == ["NoTable"]

    import csv

    rows = {r["ccn"]: r for r in csv.DictReader(open(out))}
    # Excel int 70001 -> zero-padded CCN; int ZIP 6103 -> leading-zero ZIP.
    assert rows["070001"]["zip"] == "06103"
    # Alphanumeric federal CCN upper-cased, not padded further.
    assert "17013F" in rows
    assert rows["170012"]["hospital_name"] == "Sunflower General Hospital"
    assert all(r["address"] == "" for r in rows.values())

    no_ccn = list(csv.DictReader(open(sidecar)))
    assert len(no_ccn) == 1
    assert no_ccn[0]["hospital_name"] == "Satellite Campus (no CCN)"


def test_normalizers():
    assert conv.normalize_ccn(170) == "000170"
    assert conv.normalize_ccn(" 02013f ") == "02013F"
    assert conv.normalize_zip(6103) == "06103"
    assert conv.normalize_zip("67214-1234") == "67214"
    assert conv.clean("  Two   Words ") == "Two Words"
