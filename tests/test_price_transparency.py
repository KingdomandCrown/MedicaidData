import datetime as dt
import os
from decimal import Decimal

import pytest

from hospitals import price_transparency as pt


@pytest.fixture
def tall_csv():
    return os.path.join(os.path.dirname(__file__), "fixtures", "mrf_tall_sample.csv")


@pytest.fixture
def wide_csv():
    return os.path.join(os.path.dirname(__file__), "fixtures", "mrf_wide_sample.csv")


def test_ein_from_filename_strips_upload_hash():
    assert pt.ein_from_filename("cb287b80-386006309_umich_standardcharges.zip") == "386006309"
    assert pt.ein_from_filename("520591656_hopkins_standardcharges.csv") == "520591656"
    assert pt.ein_from_filename("no-digits.csv") is None


def test_to_decimal_and_int():
    assert pt.to_decimal("$1,234.56") == Decimal("1234.56")
    assert pt.to_decimal("") is None
    assert pt.to_decimal("N/A") is None
    assert pt.to_int("12") == 12
    assert pt.to_int("") is None


def test_detect_layout():
    assert pt.detect_layout(["description", "payer_name", "plan_name"]) == "tall"
    assert (
        pt.detect_layout(
            ["description", "standard_charge|AETNA|AETNA PPO|negotiated_dollar"]
        )
        == "wide"
    )


def test_parse_payer_column():
    assert pt._parse_payer_column(
        "standard_charge|HAP [1023]|HAP HMO [102304]|negotiated_dollar"
    ) == ("HAP [1023]", "HAP HMO [102304]", "negotiated_dollar")
    assert pt._parse_payer_column("median_amount|HAP [1023]|HAP HMO [102304]") == (
        "HAP [1023]",
        "HAP HMO [102304]",
        "median_amount",
    )
    assert pt._parse_payer_column("standard_charge|gross") is None
    assert pt._parse_payer_column("description") is None


def test_read_tall_metadata_and_rows(tall_csv):
    meta, rows = pt.read_mrf(tall_csv)
    assert meta.layout == "tall"
    assert meta.hospital_name == "Sunflower General Hospital"
    assert meta.primary_npi == "1578597993"
    assert meta.ein is None or meta.ein.isdigit()  # fixture name has no EIN
    assert meta.license_number == "30034"
    assert meta.license_state == "MD"
    assert meta.last_updated_on == dt.date(2026, 4, 1)

    rows = list(rows)
    assert len(rows) == 3  # CT(Aetna), CT(BCBS), Metformin(UHC) — already tall
    first = rows[0]
    assert first.code == "70450"
    assert first.code_type == "CPT"
    assert first.payer_name == "Aetna"
    assert first.gross_charge == Decimal("1200.00")
    assert first.negotiated_dollar == Decimal("640.50")
    assert first.min_charge == Decimal("500.00")

    # The percentage-based BCBS row.
    bcbs = rows[1]
    assert bcbs.payer_name == "BCBS"
    assert bcbs.negotiated_percentage == Decimal("53.5")
    assert bcbs.negotiated_dollar is None

    # Drug row with a secondary code preserved.
    drug = rows[2]
    assert drug.code == "J8499"
    assert drug.additional_codes == "RC:0250"
    assert drug.drug_unit_of_measurement == "1"


def test_read_wide_unpivots_payers(wide_csv):
    meta, rows = pt.read_mrf(wide_csv)
    assert meta.layout == "wide"
    assert meta.npis == ["1003878539", "1790748333"]
    assert meta.primary_npi == "1003878539"
    assert meta.license_number == "1060000024"  # quotes + embedded state stripped
    assert meta.license_state == "MI"

    rows = list(rows)
    # Widget A -> 2 payer rows; Widget B -> 1 payer row (HAP skipped);
    # Widget C -> 1 baseline row (no payers). Total 4.
    assert len(rows) == 4

    widget_a = [r for r in rows if r.description == "Widget A"]
    assert len(widget_a) == 2
    payers = {r.payer_name: r for r in widget_a}
    assert set(payers) == {"HAP [1023]", "AETNA [1003]"}
    assert payers["HAP [1023]"].negotiated_dollar == Decimal("188.82")
    assert payers["HAP [1023]"].plan_name == "HAP HMO [102304]"
    assert payers["HAP [1023]"].median_amount == Decimal("257.94")
    assert payers["HAP [1023]"].count == 10
    # Item-level gross/cash denormalized onto every payer row.
    assert payers["AETNA [1003]"].gross_charge == Decimal("343.93")
    assert payers["AETNA [1003]"].discounted_cash == Decimal("137.57")

    widget_b = [r for r in rows if r.description == "Widget B"]
    assert len(widget_b) == 1  # only AETNA had a rate
    assert widget_b[0].payer_name == "AETNA [1003]"
    assert widget_b[0].negotiated_dollar == Decimal("75.00")

    widget_c = [r for r in rows if r.description == "Widget C"]
    assert len(widget_c) == 1  # baseline row, no payer
    assert widget_c[0].payer_name is None
    assert widget_c[0].gross_charge == Decimal("20.00")


def test_limit_caps_items(wide_csv):
    _, rows = pt.read_mrf(wide_csv, limit=1)
    rows = list(rows)
    # Only the first item (Widget A) is read -> its 2 payer rows.
    assert {r.description for r in rows} == {"Widget A"}
