"""Regression for a hospital's raw chargemaster export instead of the CMS template.

Great River Medical Center and South Mississippi County Regional Medical
Center both publish this vendor's column names ("Insurance Name", "Bill Code
Description", "Payor Rate", ...) instead of the CMS template's, so the header
scanner found no recognizable data header at all and refused the file.
"""

import os

from hospitals import price_transparency as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
VENDOR = os.path.join(FIX, "mrf_chargemaster_vendor_sample.csv")


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
