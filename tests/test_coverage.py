"""Why one named hospital shows no prices.

"We have 335 HCA files, so why does HCA Florida JFK show nothing?" has three
possible answers that look identical from the aggregate numbers, and lead to
three different fixes. This tells them apart.
"""

import datetime as dt

import pytest
from sqlalchemy import insert

from hospitals.coverage import coverage_for
from hospitals.db import charge_sources, hospitals, init_db, make_engine

NOW = dt.datetime(2026, 8, 18)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'c.sqlite'}")
    init_db(eng)
    return eng


def _hospital(engine, ccn, name, state="FL"):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            dict(ccn=ccn, name=name, state=state, is_active=True, ingested_at=NOW),
        )


def _source(engine, source_file, *, name=None, ccn=None, method=None, count=1000):
    with engine.begin() as conn:
        conn.execute(
            insert(charge_sources),
            dict(
                source_file=source_file,
                hospital_name=name,
                ccn=ccn,
                link_method=method,
                charge_count=count,
                ingested_at=NOW,
            ),
        )


# --- the three situations -------------------------------------------------


def test_no_file_at_all_says_so(engine):
    _hospital(engine, "100001", "HCA FLORIDA JFK HOSPITAL")

    report = coverage_for(engine, "jfk")
    assert report.sources == []
    assert len(report.hospitals) == 1
    assert "nobody has downloaded one yet" in report.diagnosis


def test_a_file_held_but_unlinked_says_so(engine):
    _hospital(engine, "100001", "HCA FLORIDA JFK HOSPITAL")
    _source(engine, "59-1479652_HCA-FLORIDA-JFK_standardcharges.json", name="HCA FLORIDA JFK")

    report = coverage_for(engine, "jfk")
    assert len(report.sources) == 1
    assert report.linked_sources == []
    assert "nothing can find them" in report.diagnosis
    assert report.unlinked_rows == 1000


def test_a_linked_file_reports_the_hospital_as_covered(engine):
    _hospital(engine, "100001", "HCA FLORIDA JFK HOSPITAL")
    _source(
        engine,
        "59-1479652_HCA-FLORIDA-JFK_standardcharges.json",
        name="HCA FLORIDA JFK",
        ccn="100001",
        method="crosswalk_npi",
    )

    report = coverage_for(engine, "jfk")
    assert report.linked_sources[0].ccn == "100001"
    assert report.covered_hospitals[0].charge_rows == 1000
    assert "all 1 matching hospital(s) have a file" in report.diagnosis


def test_a_sibling_holding_the_dataset_leaves_the_others_uncovered(engine):
    """HCA publishes one dataset per EIN; only one facility gets the link."""

    _hospital(engine, "100001", "HCA FLORIDA JFK HOSPITAL")
    _hospital(engine, "100002", "HCA FLORIDA PALMS WEST HOSPITAL")
    _source(
        engine,
        "59-1479652_HCA-FLORIDA-JFK_standardcharges.json",
        name="HCA FLORIDA JFK",
        ccn="100001",
        method="crosswalk_npi",
    )

    report = coverage_for(engine, "hca florida")
    assert len(report.covered_hospitals) == 1
    assert len(report.uncovered_hospitals) == 1
    assert report.uncovered_hospitals[0].name.endswith("PALMS WEST HOSPITAL")
    assert "share a dataset published under a sibling" in report.diagnosis


def test_an_unknown_name_matches_nothing(engine):
    report = coverage_for(engine, "nowhere general")
    assert "nothing matches that name" in report.diagnosis


# --- matching -------------------------------------------------------------


def test_the_filename_is_searched_as_well_as_the_recorded_name(engine):
    """A system file names the system inside and the facility in the filename."""

    _source(
        engine,
        "84-1321373_HCA-HEALTHONE-SKY-RIDGE_standardcharges.json",
        name="HEALTHONE",  # the file's own metadata names the system
    )

    assert len(coverage_for(engine, "sky-ridge").sources) == 1
    assert len(coverage_for(engine, "healthone").sources) == 1


def test_matching_ignores_case(engine):
    _hospital(engine, "100001", "HCA FLORIDA JFK HOSPITAL")
    assert len(coverage_for(engine, "HcA fLoRiDa").hospitals) == 1


def test_hospitals_can_be_narrowed_to_one_state(engine):
    _hospital(engine, "100001", "HCA FLORIDA JFK HOSPITAL", state="FL")
    _hospital(engine, "450001", "HCA HOUSTON HEALTHCARE", state="TX")

    assert len(coverage_for(engine, "hca").hospitals) == 2
    assert len(coverage_for(engine, "hca", state="TX").hospitals) == 1


# --- ordering -------------------------------------------------------------


def test_the_biggest_files_are_listed_first(engine):
    _source(engine, "a_hca.json", name="HCA", count=10)
    _source(engine, "b_hca.json", name="HCA", count=9_000_000)

    assert [s.charge_count for s in coverage_for(engine, "hca").sources] == [9_000_000, 10]


def test_uncovered_hospitals_are_listed_before_covered_ones(engine):
    """The ones missing data are what you came to look at."""

    _hospital(engine, "100001", "HCA ALPHA")
    _hospital(engine, "100002", "HCA BETA")
    _source(engine, "alpha.json", name="HCA ALPHA", ccn="100001", method="crosswalk_npi")

    names = [h.name for h in coverage_for(engine, "hca").hospitals]
    assert names == ["HCA BETA", "HCA ALPHA"]


def test_totals_add_up(engine):
    _source(engine, "a_hca.json", name="HCA", count=1000, ccn="100001", method="name_state")
    _source(engine, "b_hca.json", name="HCA", count=2500)

    report = coverage_for(engine, "hca")
    assert report.charge_rows == 3500
    assert report.unlinked_rows == 2500


# --- matching a word, not a fragment of one -------------------------------


def test_hca_does_not_match_healthcare(engine):
    """Searching 'hca' returned 174 hospitals: heal-THCA-re matches too.

    Adventist HealthCare, Barrett Hospital & Healthcare, Jefferson Healthcare —
    a list with no information in it.
    """

    _hospital(engine, "210060", "ADVENTIST HEALTHCARE FORT WASHINGTON")
    _hospital(engine, "501323", "JEFFERSON HEALTHCARE", state="WA")
    _hospital(engine, "100080", "HCA FLORIDA JFK HOSPITAL")

    names = [h.name for h in coverage_for(engine, "hca").hospitals]
    assert names == ["HCA FLORIDA JFK HOSPITAL"]


def test_a_word_after_a_hyphen_or_underscore_still_matches(engine):
    """Filenames are held together by punctuation, not spaces."""

    _source(engine, "62-1694180_HCA-FLORIDA-JFK-HOSPITAL_standardcharges.json", name="HCA")

    assert len(coverage_for(engine, "hca").sources) == 1
    assert len(coverage_for(engine, "florida").sources) == 1
    assert len(coverage_for(engine, "jfk").sources) == 1


def test_a_multi_word_phrase_still_matches(engine):
    _hospital(engine, "100080", "HCA FLORIDA JFK HOSPITAL")
    assert len(coverage_for(engine, "hca florida").hospitals) == 1


def test_a_fragment_inside_a_word_finds_nothing(engine):
    _hospital(engine, "100080", "HCA FLORIDA JFK HOSPITAL")
    assert coverage_for(engine, "lorida").hospitals == []


def test_a_pattern_with_regex_characters_is_taken_literally(engine):
    _hospital(engine, "100080", "ST. LUKE'S HOSPITAL")
    assert len(coverage_for(engine, "st.").hospitals) == 1
    assert coverage_for(engine, "st?").hospitals == []


def test_the_limit_applies_after_sorting(engine):
    for n in range(5):
        _source(engine, f"hca-{n}.json", name="HCA", count=n * 100)

    sources = coverage_for(engine, "hca", limit=2).sources
    assert [s.charge_count for s in sources] == [400, 300]


# --- a file with no CCN of its own ----------------------------------------


def test_a_freestanding_ed_is_not_reported_as_a_gap(engine):
    """South Parker ER holds 3,026,183 rows — Sky Ridge's count, to the digit.

    It runs under Sky Ridge's licence, has no CCN, and publishes its parent's
    chargemaster. Calling that an unlinked gap makes correct behaviour look
    broken and buries the real gaps among it.
    """

    _hospital(engine, "060112", "HCA HEALTHONE SKY RIDGE", state="CO")
    _source(
        engine,
        "84-1321373_HCA-HEALTHONE-SKY-RIDGE_standardcharges.json",
        name="HEALTHONE",
        ccn="060112",
        method="name_state",
        count=3_026_183,
    )
    _source(
        engine,
        "84-1321373_HCA-HEALTHONE-SOUTH-PARKER-ER_standardcharges.json",
        name="HEALTHONE",
        count=3_026_183,
    )
    with engine.begin() as conn:
        conn.execute(
            charge_sources.update().values(ein="841321373")
        )

    report = coverage_for(engine, "healthone")
    shared = report.shared_sources

    assert len(shared) == 1
    assert shared[0].source_file.endswith("SOUTH-PARKER-ER_standardcharges.json")
    assert shared[0].shares_ccn == "060112"
    assert shared[0].is_accounted_for is True
    assert report.missing_sources == []
    assert report.missing_rows == 0


def test_a_different_row_count_under_one_ein_is_still_a_gap(engine):
    """Presbyterian St Luke's has its own data — 3,119,832 rows, unique."""

    _hospital(engine, "060112", "HCA HEALTHONE SKY RIDGE", state="CO")
    _source(
        engine,
        "84-1321373_HCA-HEALTHONE-SKY-RIDGE_standardcharges.json",
        ccn="060112",
        method="name_state",
        count=3_026_183,
    )
    _source(
        engine,
        "84-1321373_HCA-HEALTHONE-PRESBYTERIAN-ST-LUKES_standardcharges.json",
        count=3_119_832,
    )
    with engine.begin() as conn:
        conn.execute(charge_sources.update().values(ein="841321373"))

    report = coverage_for(engine, "healthone")
    assert len(report.missing_sources) == 1
    assert report.missing_rows == 3_119_832
    assert report.shared_sources == []


def test_a_shared_count_under_a_different_ein_is_not_a_sibling(engine):
    _hospital(engine, "060112", "ALPHA HOSPITAL", state="CO")
    _source(engine, "111111111_alpha.json", ccn="060112", method="name_state", count=500)
    _source(engine, "222222222_beta.json", count=500)
    with engine.begin() as conn:
        conn.execute(
            charge_sources.update()
            .where(charge_sources.c.source_file == "111111111_alpha.json")
            .values(ein="111111111")
        )
        conn.execute(
            charge_sources.update()
            .where(charge_sources.c.source_file == "222222222_beta.json")
            .values(ein="222222222")
        )

    report = coverage_for(engine, "alpha")
    assert report.shared_sources == []
