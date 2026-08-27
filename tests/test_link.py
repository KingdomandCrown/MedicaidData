import datetime as dt
import os

import pytest
from sqlalchemy import insert, select

from hospitals.db import charge_sources, hospitals, init_db, make_engine
from hospitals.ingest_charges import ingest_charge_file
from hospitals.link import link_charges, load_crosswalk, normalize_name

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
TALL = os.path.join(FIX, "mrf_tall_sample.csv")


def _insert_hospital(engine, ccn, name, state):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            {
                "ccn": ccn,
                "name": name,
                "state": state,
                "is_active": True,
                "ingested_at": dt.datetime(2026, 1, 1),
            },
        )


def test_normalize_name_drops_noise():
    assert normalize_name("The Johns Hopkins Hospital, Inc.") == "JOHNS HOPKINS HOSPITAL"
    assert normalize_name("SUNFLOWER GENERAL HOSPITAL") == "SUNFLOWER GENERAL HOSPITAL"
    assert normalize_name(None) == ""


def test_link_by_crosswalk_npi(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'x.sqlite'}"
    engine = make_engine(db_url)
    init_db(engine)
    # POS hospital with a name that does NOT match, so only the NPI can link it.
    _insert_hospital(engine, "210009", "DIFFERENT NAME MEDICAL CENTER", "MD")
    ingest_charge_file(TALL, database_url=db_url, engine=engine)

    xwalk = tmp_path / "xwalk.csv"
    xwalk.write_text("npi,ccn,name\n1578597993,210009,The Johns Hopkins Hospital\n")
    assert load_crosswalk(engine, str(xwalk)) == 1

    summary = link_charges(engine)
    assert summary.by_crosswalk == 1
    assert summary.by_name == 0

    with engine.connect() as conn:
        row = conn.execute(
            select(charge_sources.c.ccn, charge_sources.c.link_method)
        ).one()
    assert row.ccn == "210009"
    assert row.link_method == "crosswalk_npi"


def test_link_by_name_state_fallback(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'n.sqlite'}"
    engine = make_engine(db_url)
    init_db(engine)
    # Name matches the MRF hospital ("Sunflower General Hospital"), state MD.
    _insert_hospital(engine, "210500", "Sunflower General Hospital", "MD")
    ingest_charge_file(TALL, database_url=db_url, engine=engine)

    summary = link_charges(engine)  # no crosswalk loaded
    assert summary.by_crosswalk == 0
    assert summary.by_name == 1

    with engine.connect() as conn:
        row = conn.execute(
            select(charge_sources.c.ccn, charge_sources.c.link_method)
        ).one()
    assert row.ccn == "210500"
    assert row.link_method == "name_state"


def test_crosswalk_wins_over_name(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'w.sqlite'}"
    engine = make_engine(db_url)
    init_db(engine)
    # Both a name match (210500) and a crosswalk match (210009) exist.
    _insert_hospital(engine, "210500", "Sunflower General Hospital", "MD")
    _insert_hospital(engine, "210009", "Unrelated Hospital", "MD")
    ingest_charge_file(TALL, database_url=db_url, engine=engine)
    xwalk = tmp_path / "x.csv"
    xwalk.write_text("npi,ccn\n1578597993,210009\n")
    load_crosswalk(engine, str(xwalk))

    link_charges(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(charge_sources.c.ccn, charge_sources.c.link_method)
        ).one()
    assert row.ccn == "210009"  # crosswalk takes precedence
    assert row.link_method == "crosswalk_npi"


def test_ambiguous_name_not_linked(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'a.sqlite'}"
    engine = make_engine(db_url)
    init_db(engine)
    # Two MD hospitals share the normalized name -> ambiguous, no link.
    _insert_hospital(engine, "210500", "Sunflower General Hospital", "MD")
    _insert_hospital(engine, "210501", "Sunflower General Hospital", "MD")
    ingest_charge_file(TALL, database_url=db_url, engine=engine)

    summary = link_charges(engine)
    assert summary.by_name == 0
    assert summary.unlinked == 1

    with engine.connect() as conn:
        ccn = conn.execute(select(charge_sources.c.ccn)).scalar_one()
    assert ccn is None


def test_no_name_fallback_only_crosswalk(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'c.sqlite'}"
    engine = make_engine(db_url)
    init_db(engine)
    _insert_hospital(engine, "210500", "Sunflower General Hospital", "MD")
    ingest_charge_file(TALL, database_url=db_url, engine=engine)

    summary = link_charges(engine, use_name_fallback=False)
    assert summary.by_name == 0
    assert summary.unlinked == 1


def test_load_crosswalk_rejects_bad_columns(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'b.sqlite'}"
    engine = make_engine(db_url)
    init_db(engine)
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError):
        load_crosswalk(engine, str(bad))


def test_an_apostrophe_does_not_break_a_name_in_two():
    """POS writes "ST LUKE'S"; the filename writes "ST-LUKES".

    Turning the apostrophe into a space made those "LUKE S" and "LUKES" —
    different names, and 3.1 million charge rows unmatched over one character.
    """

    from hospitals.link import normalize_name

    assert normalize_name("HCA HEALTHONE PRESBYTERIAN ST LUKE'S") == normalize_name(
        "HCA-HEALTHONE-PRESBYTERIAN-ST-LUKES"
    )
    assert normalize_name("CHILDREN'S MERCY") == normalize_name("CHILDRENS MERCY")
    # A typographic apostrophe is the same character to a reader.
    assert normalize_name("ST MARY’S") == normalize_name("ST MARYS")


def test_other_punctuation_still_separates_words():
    from hospitals.link import normalize_name

    assert normalize_name("ST. LUKE'S-ROOSEVELT") == "ST LUKES ROOSEVELT"
    assert normalize_name("MERCY/ST VINCENT") == "MERCY ST VINCENT"


# --- a file we downloaded ourselves ---------------------------------------


@pytest.fixture
def linked(tmp_path):
    """An empty database plus the two helpers these tests need."""

    engine = make_engine(f"sqlite:///{tmp_path / 'l.sqlite'}")
    init_db(engine)
    return engine


def _add_source(engine, source_file, **extra):
    row = {
        "source_file": source_file,
        "charge_count": 1,
        "ingested_at": dt.datetime(2026, 1, 1),
    }
    row.update(extra)
    with engine.begin() as conn:
        conn.execute(insert(charge_sources), row)


def _linked_as(engine, source_file):
    with engine.connect() as conn:
        return conn.execute(
            select(charge_sources.c.ccn, charge_sources.c.link_method).where(
                charge_sources.c.source_file == source_file
            )
        ).one()


def test_a_downloaded_file_links_by_the_ccn_in_its_name(linked):
    """Discovery started from the hospital, so the owner was known before the
    URL was. That is a fact, not a heuristic, and should be used as one."""

    _insert_hospital(linked, "170027", "PRATT REGIONAL MEDICAL CENTER", "KS")
    name = "ccn-170027_pratt-regional-medical-center_standardcharges.json"
    _add_source(linked, name, hospital_name="SOMETHING ELSE ENTIRELY")

    summary = link_charges(linked)

    assert summary.by_filename == 1
    assert summary.unlinked == 0
    assert _linked_as(linked, name) == ("170027", "filename_ccn")


def test_the_filename_ccn_wins_over_the_name_heuristic(linked):
    """The heuristic reconstructs after the fact what the filename already knows."""

    _insert_hospital(linked, "170027", "PRATT REGIONAL MEDICAL CENTER", "KS")
    _insert_hospital(linked, "170045", "VIA CHRISTI ST FRANCIS", "KS")
    _add_source(
        linked,
        "ccn-170027_x_standardcharges.json",
        hospital_name="VIA CHRISTI ST FRANCIS",
        license_state="KS",
    )

    link_charges(linked)
    assert _linked_as(linked, "ccn-170027_x_standardcharges.json").ccn == "170027"


def test_a_ccn_no_hospital_has_is_not_invented(linked):
    """A hand-renamed file must not conjure a hospital that does not exist."""

    _insert_hospital(linked, "170027", "PRATT REGIONAL MEDICAL CENTER", "KS")
    _add_source(linked, "ccn-999999_made-up_standardcharges.json")

    summary = link_charges(linked)
    assert summary.by_filename == 0
    assert _linked_as(linked, "ccn-999999_made-up_standardcharges.json").ccn is None


def test_an_ordinary_filename_is_untouched_by_this(linked):
    _insert_hospital(linked, "170027", "PRATT REGIONAL MEDICAL CENTER", "KS")
    _add_source(
        linked,
        "480598437_PRATT-REGIONAL_standardcharges.json",
        hospital_name="PRATT REGIONAL MEDICAL CENTER",
        license_state="KS",
    )

    summary = link_charges(linked)
    assert summary.by_filename == 0
    assert summary.by_name == 1
