import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from hospitals import price_transparency as pt
from hospitals.db import charge_sources, make_engine, standard_charges
from hospitals.ingest_charges import ingest_charge_file

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def json_mrf():
    return os.path.join(FIX, "mrf_sample.json")


def test_ein_from_dashed_filename():
    assert pt.ein_from_filename("24-0795959_Geisinger_standardcharges.csv") == "240795959"
    assert pt.ein_from_filename("956006143_ucla_standardcharges.json") == "956006143"


def test_read_json_metadata(json_mrf):
    meta, _ = pt.read_mrf_json(json_mrf)
    assert meta.layout == "json"
    assert meta.hospital_name == "Cascade Regional Medical Center"
    assert meta.version == "2.2.0"
    assert meta.last_updated_on == dt.date(2026, 4, 1)
    assert meta.license_number == "WA-12345"
    assert meta.license_state == "WA"
    assert meta.hospital_address == "500 Health Way, Seattle, WA 98101"


def test_read_json_rows(json_mrf):
    _, rows = pt.read_mrf_json(json_mrf)
    rows = list(rows)
    # MRI: 2 payers -> 2 rows; Ibuprofen: no payers -> 1 baseline row. Total 3.
    assert len(rows) == 3

    mri = [r for r in rows if r.description == "MRI brain without contrast"]
    assert len(mri) == 2
    assert mri[0].code == "70551"
    assert mri[0].code_type == "CPT"
    assert mri[0].additional_codes == "RC:0350"
    assert mri[0].modifiers == "26"
    assert mri[0].gross_charge == Decimal("3200.00")
    assert mri[0].min_charge == Decimal("1400.00")

    payers = {r.payer_name: r for r in mri}
    assert payers["Premera"].negotiated_dollar == Decimal("1850.50")
    assert payers["Premera"].median_amount == Decimal("1900.00")  # estimated_amount
    assert payers["Regence"].negotiated_percentage == Decimal("62.5")
    assert payers["Regence"].negotiated_algorithm == "62.5% of billed charges"

    drug = [r for r in rows if r.description == "Ibuprofen 200mg tablet"]
    assert len(drug) == 1
    assert drug[0].payer_name is None  # baseline row, no payer
    assert drug[0].drug_unit_of_measurement == "1"
    assert drug[0].gross_charge == Decimal("8.00")


def test_read_any_dispatches_json(json_mrf):
    meta, rows = pt.read_any(json_mrf)
    assert meta.layout == "json"
    assert len(list(rows)) == 3


def test_json_limit(json_mrf):
    _, rows = pt.read_mrf_json(json_mrf, limit=1)
    rows = list(rows)
    assert {r.description for r in rows} == {"MRI brain without contrast"}


def test_ingest_json_end_to_end(tmp_path, json_mrf):
    db_url = f"sqlite:///{tmp_path / 'j.sqlite'}"
    summary = ingest_charge_file(json_mrf, database_url=db_url)
    assert summary.layout == "json"
    assert summary.charges_loaded == 3

    engine = make_engine(db_url)
    with engine.connect() as conn:
        src = conn.execute(select(charge_sources)).mappings().one()
        assert src["layout"] == "json"
        assert src["license_state"] == "WA"
        n = conn.execute(
            select(func.count()).select_from(standard_charges)
        ).scalar_one()
        assert n == 3


def test_a_json_that_is_not_an_mrf_is_rejected(tmp_path):
    """A tool's own data landing in the folder must not load as an empty hospital."""

    import json
    import zipfile

    not_mrf = tmp_path / "MinervaAI_Dell_Extractor.json"
    not_mrf.write_text(json.dumps({"version": "1.0", "files": ["a", "b"]}))
    with pytest.raises(ValueError, match="not a CMS price transparency file"):
        pt.read_any(str(not_mrf))

    zipped = tmp_path / "MinervaAI_Dell_Extractor.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"tool": "extractor"}))
    with pytest.raises(ValueError, match="not a CMS price transparency file"):
        pt.read_any(str(zipped))


def test_a_real_mrf_json_is_still_accepted(json_mrf):
    meta, rows = pt.read_any(json_mrf)
    assert meta.layout == "json"
    assert list(rows)
