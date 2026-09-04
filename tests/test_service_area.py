"""Loading and reading patient origins.

The tests that matter most here are about suppression. CMS withholds cells
below its publication threshold, and every one of them read as a zero is a
rural hospital that appears to serve nobody in a ZIP it actually serves. There
is no error when that goes wrong -- the map just quietly shrinks.
"""

import datetime as dt

import pytest
from sqlalchemy import insert, select

from hospitals.db import hospital_service_area, hospitals, init_db, make_engine
from hospitals.service_area import (
    competitors,
    draw_area,
    fetch_service_area,
    normalize_ccn,
    normalize_zip,
    parse_amount,
    parse_count,
    write_rows,
)

NOW = dt.datetime(2026, 9, 4)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 't.sqlite'}")
    init_db(eng)
    return eng


def _hospital(engine, ccn, name, state="KS"):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            dict(ccn=ccn, name=name, state=state, is_active=True, ingested_at=NOW),
        )


def _area(engine, ccn, zip5, cases, *, suppressed=False, edition="2024"):
    with engine.begin() as conn:
        conn.execute(
            insert(hospital_service_area),
            dict(
                ccn=ccn,
                zip5=zip5,
                edition=edition,
                cases=cases,
                days=None,
                charges=None,
                suppressed=suppressed,
                source="test",
                loaded_at=NOW,
            ),
        )


# --- suppression ------------------------------------------------------------


def test_a_withheld_cell_is_not_a_zero():
    """The single rule this file demands."""

    assert parse_count("*") == (None, True)
    assert parse_count("**") == (None, True)
    assert parse_count("") == (None, True)
    assert parse_count("N/A") == (None, True)


def test_a_real_zero_is_kept_as_a_zero():
    assert parse_count("0") == (0, False)


def test_ordinary_counts_survive_their_formatting():
    assert parse_count("1,234") == (1234, False)
    assert parse_count(" 88 ") == (88, False)


def test_an_unparseable_count_is_treated_as_withheld_not_zero():
    """Guessing zero from junk makes the same mistake with less excuse."""

    assert parse_count("about twelve") == (None, True)


def test_amounts_lose_their_punctuation():
    assert parse_amount("$1,234.50") == 1234.50
    assert parse_amount("*") is None


# --- identifiers ------------------------------------------------------------


def test_a_ccn_stripped_of_its_leading_zero_is_recovered():
    """A spreadsheet in the chain turns 070027 into 70027."""

    assert normalize_ccn("70027") == "070027"
    assert normalize_ccn("070027") == "070027"


def test_something_that_is_not_a_ccn_is_dropped():
    assert normalize_ccn("") is None
    assert normalize_ccn("NOT-A-CCN") is None
    assert normalize_ccn("1234567") is None


def test_a_zip_keeps_its_leading_zero():
    assert normalize_zip("1002") == "01002"
    assert normalize_zip("66502") == "66502"


def test_zip_plus_four_is_truncated_to_five():
    assert normalize_zip("665021234") == "66502"
    assert normalize_zip("66502-1234") == "66502"


def test_a_missing_zip_is_none_not_zeros():
    assert normalize_zip("") is None
    assert normalize_zip(None) is None


# --- writing ----------------------------------------------------------------


def test_reloading_an_edition_replaces_it_rather_than_doubling_it(engine):
    rows = [
        dict(ccn="170027", zip5="67124", edition="2024", cases=50, days=None,
             charges=None, suppressed=False, source="t", loaded_at=NOW),
    ]
    write_rows(engine, rows)
    write_rows(engine, rows)

    with engine.connect() as conn:
        all_rows = conn.execute(select(hospital_service_area.c.cases)).all()
    assert len(all_rows) == 1


def test_a_second_edition_lives_alongside_the_first(engine):
    base = dict(zip5="67124", cases=50, days=None, charges=None,
                suppressed=False, source="t", loaded_at=NOW)
    write_rows(engine, [dict(base, ccn="170027", edition="2023")])
    write_rows(engine, [dict(base, ccn="170027", edition="2024")])

    with engine.connect() as conn:
        editions = {r.edition for r in conn.execute(
            select(hospital_service_area.c.edition))}
    assert editions == {"2023", "2024"}


# --- reading it back --------------------------------------------------------


def test_a_hospitals_draw_is_ranked_by_volume(engine):
    _hospital(engine, "170027", "PRATT")
    _area(engine, "170027", "67124", 300)
    _area(engine, "170027", "67501", 40)

    draw = draw_area(engine, "170027")

    assert [d.zip5 for d in draw] == ["67124", "67501"]


def test_share_is_of_the_zip_not_of_the_hospital(engine):
    _hospital(engine, "170027", "PRATT")
    _hospital(engine, "170045", "RIVAL")
    _area(engine, "170027", "67124", 75)
    _area(engine, "170045", "67124", 25)

    draw = draw_area(engine, "170027")

    assert draw[0].zip_total == 100
    assert draw[0].share == 0.75


def test_a_suppressed_cell_does_not_deflate_a_rivals_share(engine):
    """Counting it as zero would inflate the published hospital to 100%."""

    _hospital(engine, "170027", "PRATT")
    _hospital(engine, "170045", "RIVAL")
    _area(engine, "170027", "67124", 75)
    _area(engine, "170045", "67124", None, suppressed=True)

    draw = draw_area(engine, "170027")

    assert draw[0].zip_total == 75
    assert draw[0].share == 1.0


def test_a_hospital_with_only_suppressed_cells_has_no_draw_not_a_zero_draw(engine):
    _hospital(engine, "170099", "TINY")
    _area(engine, "170099", "67124", None, suppressed=True)

    assert draw_area(engine, "170099") == []


def test_an_unknown_hospital_returns_nothing(engine):
    assert draw_area(engine, "999999") == []
    assert competitors(engine, "999999") == []


def test_competitors_are_the_hospitals_sharing_your_zips(engine):
    _hospital(engine, "170027", "PRATT")
    _hospital(engine, "170045", "RIVAL")
    _hospital(engine, "170099", "FAR AWAY")
    _area(engine, "170027", "67124", 300)
    _area(engine, "170045", "67124", 120)
    _area(engine, "170099", "99999", 500)

    rivals = competitors(engine, "170027")

    assert [c.ccn for c in rivals] == ["170045"]
    assert rivals[0].name == "RIVAL"
    assert rivals[0].overlap_cases == 120
    assert rivals[0].shared_zips == 1


def test_competitors_are_ranked_by_cases_taken_in_your_area(engine):
    _hospital(engine, "170027", "PRATT")
    _hospital(engine, "170045", "SMALL RIVAL")
    _hospital(engine, "170055", "BIG RIVAL")
    _area(engine, "170027", "67124", 300)
    _area(engine, "170045", "67124", 50)
    _area(engine, "170055", "67124", 400)

    assert [c.ccn for c in competitors(engine, "170027")] == ["170055", "170045"]


def test_a_competitor_missing_from_the_roster_still_appears(engine):
    """The file covers providers the POS roster does not; a nameless rival is
    still a rival, and dropping it would understate the market."""

    _hospital(engine, "170027", "PRATT")
    _area(engine, "170027", "67124", 300)
    _area(engine, "179999", "67124", 200)

    rivals = competitors(engine, "170027")

    assert [c.ccn for c in rivals] == ["179999"]
    assert rivals[0].name is None


# --- fetching ---------------------------------------------------------------


class _Distribution:
    title = "Hospital Service Area File 2024"
    modified = "2025-01-15"
    has_data_api = False
    download_url = "https://example.test/hsa.csv"


def _patch_cms(monkeypatch, records):
    from hospitals import service_area

    monkeypatch.setattr(
        service_area.cms_pos,
        "discover_latest_distribution",
        lambda **kw: _Distribution(),
    )
    monkeypatch.setattr(
        service_area.cms_pos,
        "iter_distribution_records",
        lambda dist, **kw: iter(records),
    )


def test_a_fetch_loads_only_hospitals_we_track(engine, monkeypatch):
    _hospital(engine, "170027", "PRATT")
    _patch_cms(monkeypatch, [
        {"MEDICARE_PROV_NUM": "170027", "ZIP_CD_OF_RESIDENCE": "67124",
         "TOTAL_CASES": "300", "TOTAL_DAYS_OF_CARE": "900",
         "TOTAL_CHARGES": "1,000,000"},
        {"MEDICARE_PROV_NUM": "450000", "ZIP_CD_OF_RESIDENCE": "75001",
         "TOTAL_CASES": "50", "TOTAL_DAYS_OF_CARE": "100",
         "TOTAL_CHARGES": "5000"},
    ])

    summary = fetch_service_area(engine)

    assert summary.loaded == 1
    assert summary.rows_read == 2
    assert summary.unknown_ccns == {"450000"}
    assert summary.hospitals_covered == 1


def test_a_fetch_counts_suppressed_cells_and_keeps_them(engine, monkeypatch):
    """Kept, because 'this ZIP was too small to publish' is itself a fact."""

    _hospital(engine, "170027", "PRATT")
    _patch_cms(monkeypatch, [
        {"MEDICARE_PROV_NUM": "170027", "ZIP_CD_OF_RESIDENCE": "67124",
         "TOTAL_CASES": "*", "TOTAL_DAYS_OF_CARE": "*", "TOTAL_CHARGES": "*"},
    ])

    summary = fetch_service_area(engine)

    assert summary.suppressed == 1
    assert summary.loaded == 1
    with engine.connect() as conn:
        row = conn.execute(select(hospital_service_area)).mappings().one()
    assert row["cases"] is None
    assert row["suppressed"] is True


def test_alternative_column_names_are_understood(engine, monkeypatch):
    """CMS renames these between editions."""

    _hospital(engine, "170027", "PRATT")
    _patch_cms(monkeypatch, [
        {"PROVIDER_ID": "170027", "ZIP_CODE": "67124", "TOTAL_DISCHARGES": "12"},
    ])

    assert fetch_service_area(engine).loaded == 1


def test_a_file_with_no_provider_column_fails_loudly(engine, monkeypatch):
    _hospital(engine, "170027", "PRATT")
    _patch_cms(monkeypatch, [{"SOMETHING": "x", "ELSE": "y"}])

    with pytest.raises(LookupError, match="provider/ZIP"):
        fetch_service_area(engine)


def test_loading_into_a_hospital_less_database_is_refused(engine, monkeypatch):
    """Every row would be unjoinable, which is nearly always a wrong database."""

    _patch_cms(monkeypatch, [])

    with pytest.raises(LookupError, match="No hospitals"):
        fetch_service_area(engine)


def test_a_repeated_provider_zip_pair_does_not_break_the_load(engine, monkeypatch):
    _hospital(engine, "170027", "PRATT")
    _patch_cms(monkeypatch, [
        {"MEDICARE_PROV_NUM": "170027", "ZIP_CD_OF_RESIDENCE": "67124",
         "TOTAL_CASES": "300"},
        {"MEDICARE_PROV_NUM": "170027", "ZIP_CD_OF_RESIDENCE": "67124",
         "TOTAL_CASES": "300"},
    ])

    assert fetch_service_area(engine).loaded == 1


def test_rows_with_no_usable_identifier_are_counted_not_loaded(engine, monkeypatch):
    _hospital(engine, "170027", "PRATT")
    _patch_cms(monkeypatch, [
        {"MEDICARE_PROV_NUM": "", "ZIP_CD_OF_RESIDENCE": "67124", "TOTAL_CASES": "1"},
        {"MEDICARE_PROV_NUM": "170027", "ZIP_CD_OF_RESIDENCE": "", "TOTAL_CASES": "1"},
    ])

    summary = fetch_service_area(engine)

    assert summary.unusable == 2
    assert summary.loaded == 0
