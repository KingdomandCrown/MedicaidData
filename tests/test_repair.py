"""Repairing EINs that a wrong parser wrote into the database.

Round 6's duplicate report showed the same Atrium file under two EINs:
``166934899`` and ``334038167``. The first is not an EIN at all — it is the
organizational NPI ``1669348991`` with its last digit chopped off, because the
filename parser matched nine digits without asking whether ten were there.

8.5 million charge rows were filed under an organization that does not exist,
which hides them from every benchmark the real hospital appears in.
"""

import datetime as dt

import pytest
from sqlalchemy import insert, select

from hospitals.db import charge_sources, init_db, make_engine, standard_charges
from hospitals.price_transparency import ein_from_filename, npi_from_filename
from hospitals.repair import find_ein_mismatches, repair_eins

NOW = dt.datetime(2026, 8, 18)

ATRIUM_NPI_ONLY = "1669348991_atrium-health-hospitals-inc_standardcharges.csv"
ATRIUM_WITH_EIN = "334038167-1669348991_atrium-health-hospitals-inc_standardcharges.csv"


# --- the parser -----------------------------------------------------------


def test_a_ten_digit_npi_is_not_read_as_a_nine_digit_ein():
    assert ein_from_filename(ATRIUM_NPI_ONLY) is None
    assert npi_from_filename(ATRIUM_NPI_ONLY) == "1669348991"


def test_an_ein_npi_pair_still_yields_the_ein():
    assert ein_from_filename(ATRIUM_WITH_EIN) == "334038167"
    assert npi_from_filename(ATRIUM_WITH_EIN) == "1669348991"


@pytest.mark.parametrize(
    "filename",
    [
        "1790727550_carolinas-rehabilitation_standardcharges.csv",
        "1336184019_good-samaritan-hospital_standardcharges.json",
        "1184622847_chi-st-lukes-health-baylor_standardcharges.json",
    ],
)
def test_the_other_npi_named_files_from_round_six_yield_no_ein(filename):
    assert ein_from_filename(filename) is None
    assert npi_from_filename(filename) is not None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("24-0795959_Geisinger_standardcharges.csv", "240795959"),
        ("386006309_hospital_standardcharges.csv", "386006309"),
        ("84-1321373_HCA-HEALTHONE-ROSE_standardcharges.json", "841321373"),
        ("261947374-1659559573_st-lukes-sugar-land_standardcharges.json", "261947374"),
        ("560529945-1487866315_the-charlotte-mecklenburg_standardcharges.csv", "560529945"),
        ("a1b2c3d4-386006309_hosp_standardcharges.csv", "386006309"),
        ("183459362_1811044878_william-beaumont_standardcharges.csv", "183459362"),
    ],
)
def test_real_eins_are_unaffected(filename, expected):
    assert ein_from_filename(filename) == expected


def test_a_nine_digit_number_starting_with_one_is_still_an_ein():
    # Only *ten* digits make an NPI. A nine-digit EIN may start with anything.
    assert ein_from_filename("133618401_some-hospital_standardcharges.csv") == "133618401"


# --- the repair -----------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'r.sqlite'}")
    init_db(eng)
    return eng


def _load(engine, spec):
    """Insert sources with the EIN the *old* parser would have stored."""

    with engine.begin() as conn:
        for source_file, stored_ein, count in spec:
            result = conn.execute(
                insert(charge_sources),
                dict(
                    source_file=source_file,
                    ein=stored_ein,
                    charge_count=count,
                    ingested_at=NOW,
                ),
            )
            source_id = int(result.inserted_primary_key[0])
            conn.execute(
                insert(standard_charges),
                [
                    {"source_id": source_id, "ein": stored_ein, "code": str(i)}
                    for i in range(count)
                ],
            )


def _stored(engine):
    with engine.connect() as conn:
        return {
            r.source_file: (r.ein, r.primary_npi)
            for r in conn.execute(
                select(
                    charge_sources.c.source_file,
                    charge_sources.c.ein,
                    charge_sources.c.primary_npi,
                )
            )
        }


def test_the_invented_ein_is_found(engine):
    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 3)])

    fixes = find_ein_mismatches(engine)
    assert len(fixes) == 1
    assert fixes[0].stored_ein == "166934899"
    assert fixes[0].correct_ein is None
    assert fixes[0].npi == "1669348991"
    assert fixes[0].drops_an_invented_ein is True


def test_correct_eins_are_left_alone(engine):
    _load(engine, [(ATRIUM_WITH_EIN, "334038167", 2), ("24-0795959_g.csv", "240795959", 2)])
    assert find_ein_mismatches(engine) == []


def test_a_dry_run_writes_nothing(engine):
    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 3)])

    summary = repair_eins(engine)
    assert summary.source_count == 1
    assert summary.applied is False
    assert summary.charge_rows_rewritten == 0
    assert _stored(engine)[ATRIUM_NPI_ONLY] == ("166934899", None)


def test_applying_clears_the_invented_ein_and_keeps_the_npi(engine):
    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 3)])

    summary = repair_eins(engine, apply=True)
    assert summary.charge_rows_rewritten == 3
    assert _stored(engine)[ATRIUM_NPI_ONLY] == (None, "1669348991")

    with engine.connect() as conn:
        eins = {r[0] for r in conn.execute(select(standard_charges.c.ein))}
    assert eins == {None}


def test_sources_only_leaves_the_charge_rows(engine):
    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 3)])

    summary = repair_eins(engine, apply=True, sources_only=True)
    assert summary.charge_rows_rewritten == 0
    assert _stored(engine)[ATRIUM_NPI_ONLY][0] is None

    with engine.connect() as conn:
        eins = {r[0] for r in conn.execute(select(standard_charges.c.ein))}
    assert eins == {"166934899"}  # still to do


def test_the_repair_is_idempotent(engine):
    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 2)])

    repair_eins(engine, apply=True)
    again = repair_eins(engine, apply=True)
    assert again.source_count == 0


def test_the_affected_row_count_is_reported_before_committing(engine):
    """The charge-row rewrite is the expensive half; its size is knowable first."""

    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 5), ("1790727550_cr.csv", "179072755", 3)])

    summary = repair_eins(engine)
    assert summary.source_count == 2
    assert summary.charge_rows_affected == 8


def test_biggest_files_are_listed_first(engine):
    _load(engine, [(ATRIUM_NPI_ONLY, "166934899", 2), ("1790727550_cr.csv", "179072755", 9)])
    assert [f.charge_count for f in find_ein_mismatches(engine)] == [9, 2]


def test_an_empty_database_has_nothing_to_repair(engine):
    assert repair_eins(engine, apply=True).source_count == 0
