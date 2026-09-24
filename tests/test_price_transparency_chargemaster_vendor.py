"""Regression for a hospital's raw chargemaster export instead of the CMS template.

Great River Medical Center and South Mississippi County Regional Medical
Center both publish this vendor's column names ("Insurance Name", "Bill Code
Description", "Payor Rate", ...) instead of the CMS template's, so the header
scanner found no recognizable data header at all and refused the file.
"""

import os

import pytest

from hospitals import price_transparency as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
VENDOR = os.path.join(FIX, "mrf_chargemaster_vendor_sample.csv")
UNRELATED_VENDOR = os.path.join(FIX, "mrf_unrelated_vendor_gross_charge_only.csv")
RAY_COUNTY = os.path.join(FIX, "mrf_ray_county_vendor_sample.csv")


def test_vendor_header_is_recognized_as_a_data_header():
    header = [
        "Insurance Name", "Plan Name", "Bill Code Type", "Bill Code",
        "Bill Code Description", "Alternate Bill Code",
        "Alternate Bill Code Description", "NDC", "Gross Charge",
    ]
    assert pt._looks_like_data_header(header)


def test_vendor_chargemaster_file_parses():
    meta, rows = pt.read_any(VENDOR)
    rows = list(rows)

    assert meta.layout == "tall"
    assert len(rows) == 3

    gross_only = rows[0]
    assert gross_only.description == "GEODON 20MG INJ"
    assert gross_only.code == "1101882"
    assert gross_only.code_type == "CHRGCD"
    assert gross_only.additional_codes == ":J3486"
    assert str(gross_only.gross_charge) == "29.50"
    assert gross_only.negotiated_dollar is None  # "Payor Rate" was "N/A"
    assert gross_only.min_charge is None
    # "Insurance Name"/"Plan Name" were the literal text "N/A", not a real payer.
    assert gross_only.payer_name is None
    assert gross_only.plan_name is None

    no_alternate_code = rows[1]
    assert no_alternate_code.code == "1101978"
    assert no_alternate_code.additional_codes is None  # blank alternate code/description

    negotiated = rows[2]
    assert negotiated.payer_name == "Aetna"
    assert negotiated.plan_name == "Commercial PPO"
    assert str(negotiated.negotiated_dollar) == "18.40"
    assert str(negotiated.min_charge) == "15.00"
    assert str(negotiated.max_charge) == "25.00"


def test_vendor_file_on_line_one_has_no_metadata_preamble():
    """No CMS metadata rows exist in this vendor's export, so identification
    falls back to the filename's EIN, same as any other header-only file."""

    meta, _rows = pt.read_any(VENDOR)
    assert meta.hospital_name is None


def test_ray_county_payer_negotiated_charge_columns_parse_as_wide():
    """"Payer Negotiated Charge: X (Plan: Y)" spells out payer and plan in the
    column name itself rather than CMS's pipe-delimited convention; rewriting
    it into that convention lets the existing wide-format grouping read it
    with no extraction code of its own."""

    meta, rows = pt.read_any(RAY_COUNTY)
    rows = list(rows)

    assert meta.layout == "wide"
    assert len(rows) == 3  # item 10 has 2 non-blank payer rates, item 100 has 1

    pentoxifylline = [r for r in rows if r.code == "10"]
    assert {(r.payer_name, r.plan_name, str(r.negotiated_dollar)) for r in pentoxifylline} == {
        ("Aetna", "Default", "1.09"),
        ("Aetna", "Medicare Advantage", "2.84"),
    }
    assert pentoxifylline[0].description == "NF-PENTOXIFYLLINE ORAL TAB 400MG"
    assert str(pentoxifylline[0].gross_charge) == "10.61"
    assert str(pentoxifylline[0].min_charge) == "1.09"
    assert str(pentoxifylline[0].max_charge) == "8.25"

    alphagan = [r for r in rows if r.code == "100"]
    assert len(alphagan) == 1  # only BCBS-KC had a value; the two Aetna cells were blank
    assert alphagan[0].payer_name == "Blue Cross Blue Shield of Kansas City"
    assert str(alphagan[0].negotiated_dollar) == "5.57"


def test_an_unrelated_vendor_with_only_a_gross_charge_column_is_not_misread():
    """A third, unrelated chargemaster export whose only overlap with the
    Great River vendor format is an ordinary "Gross Charge" column used to
    silently "succeed" with 0 rows instead of correctly failing: the alias
    to standard_charge|gross (1 pipe) was accepted as proof of a genuine CMS
    wide file, whose real per-payer columns always carry >= 2 pipes."""

    with pytest.raises(ValueError, match="data header"):
        pt.read_any(UNRELATED_VENDOR)
