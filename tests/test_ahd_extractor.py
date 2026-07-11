import importlib.util
import os

import pytest

openpyxl = pytest.importorskip("openpyxl")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "extract_ahd_websites.py")
_spec = importlib.util.spec_from_file_location("extract_ahd_websites", SCRIPT)
ahd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ahd)


def _make_profile(path, website_row=True):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Girard Medical Center"
    ws.append(["Profile - February 4, 2026"])
    ws.append([])
    ws.append(["Girard Medical Center"])
    ws.append(["Girard, KS \xa066743"])
    ws.append(["CMS Certification Number: 171376"])
    ws.append(["Identification and Characteristics"])
    ws.append(["Name and Address", "Girard Medical Center 302 North Hospital Drive"])
    ws.append(["Telephone number", "(620) 724-8291"])
    if website_row:
        ws.append(["Hospital Website", "www.girardmedicalcenter.com"])
    ws.append(["CMS Certification Number", "171376"])
    ws.append(["Operating Status", "Operating"])
    wb.create_sheet("Financial - February 4, 2026").append(["irrelevant"])
    wb.save(path)


def test_extract_file(tmp_path):
    path = tmp_path / "girard.xlsx"
    _make_profile(path)
    row = ahd.extract_file(str(path))
    assert row["ccn"] == "171376"
    assert row["hospital_name"] == "Girard Medical Center"
    assert row["website_uri"] == "https://www.girardmedicalcenter.com"
    assert row["system_website"] == ""
    assert row["operating_status"] == "Operating"
    assert row["source"] == "ahd"


def test_extract_without_website_row(tmp_path):
    path = tmp_path / "nosite.xlsx"
    _make_profile(path, website_row=False)
    row = ahd.extract_file(str(path))
    # CCN still found (banner fallback also covers it); website empty.
    assert row["ccn"] == "171376"
    assert row["website_uri"] == ""


def test_normalizers():
    assert ahd.normalize_website("example.org/x") == "https://example.org/x"
    assert ahd.normalize_website("http://example.org") == "http://example.org"
    assert ahd.normalize_ccn("171376") == "171376"
    assert ahd.normalize_ccn(170) == "000170"
