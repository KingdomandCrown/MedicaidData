import os
import shutil
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from hospitals.db import charge_sources, make_engine, standard_charges
from hospitals.ingest_charges import ingest_charge_file, ingest_charge_path

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def tall_csv():
    return os.path.join(FIX, "mrf_tall_sample.csv")


@pytest.fixture
def wide_csv():
    return os.path.join(FIX, "mrf_wide_sample.csv")


def test_ingest_tall_file(tmp_path, tall_csv):
    db_url = f"sqlite:///{tmp_path / 'c.sqlite'}"
    summary = ingest_charge_file(tall_csv, database_url=db_url)
    assert summary.layout == "tall"
    assert summary.charges_loaded == 3

    engine = make_engine(db_url)
    with engine.connect() as conn:
        src = conn.execute(select(charge_sources)).mappings().one()
        assert src["hospital_name"] == "Sunflower General Hospital"
        assert src["primary_npi"] == "1578597993"
        assert src["charge_count"] == 3
        n = conn.execute(
            select(func.count()).select_from(standard_charges)
        ).scalar_one()
        assert n == 3
        # Charges carry the denormalized NPI for later joins.
        npis = {
            r[0]
            for r in conn.execute(select(standard_charges.c.primary_npi)).all()
        }
        assert npis == {"1578597993"}


def test_ingest_wide_file(tmp_path, wide_csv):
    db_url = f"sqlite:///{tmp_path / 'w.sqlite'}"
    summary = ingest_charge_file(wide_csv, database_url=db_url)
    assert summary.layout == "wide"
    assert summary.charges_loaded == 4

    engine = make_engine(db_url)
    with engine.connect() as conn:
        val = conn.execute(
            select(standard_charges.c.negotiated_dollar).where(
                (standard_charges.c.description == "Widget A")
                & (standard_charges.c.payer_name == "HAP [1023]")
            )
        ).scalar_one()
        assert Decimal(str(val)) == Decimal("188.82")


def test_reingest_is_idempotent(tmp_path, tall_csv):
    db_url = f"sqlite:///{tmp_path / 'idem.sqlite'}"
    ingest_charge_file(tall_csv, database_url=db_url)
    ingest_charge_file(tall_csv, database_url=db_url)

    engine = make_engine(db_url)
    with engine.connect() as conn:
        sources = conn.execute(
            select(func.count()).select_from(charge_sources)
        ).scalar_one()
        charges = conn.execute(
            select(func.count()).select_from(standard_charges)
        ).scalar_one()
    assert sources == 1  # replaced, not duplicated
    assert charges == 3


def test_ingest_directory(tmp_path, tall_csv, wide_csv):
    src_dir = tmp_path / "mrfs"
    src_dir.mkdir()
    shutil.copy(tall_csv, src_dir / "111111111_a_standardcharges.csv")
    shutil.copy(wide_csv, src_dir / "222222222_b_standardcharges.csv")

    db_url = f"sqlite:///{tmp_path / 'dir.sqlite'}"
    summaries = ingest_charge_path(str(src_dir), database_url=db_url)
    assert len(summaries) == 2
    # EIN is derived from each filename.
    eins = {s.ein for s in summaries}
    assert eins == {"111111111", "222222222"}
    assert sum(s.charges_loaded for s in summaries) == 7


def test_missing_file_raises(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'x.sqlite'}"
    with pytest.raises(FileNotFoundError):
        ingest_charge_file(str(tmp_path / "nope.csv"), database_url=db_url)
