"""The download worklist: what is still missing, and what to fetch first."""

import datetime as dt
import os

import pytest
from sqlalchemy import insert

from hospitals.db import charge_sources, hospitals, init_db, make_engine
from hospitals.gap import build_gap_report, system_key, write_xlsx

NOW = dt.datetime(2026, 8, 13)


def _hospital(ccn, name, state, city="Town", active=True, beds=25):
    return dict(
        ccn=ccn,
        name=name,
        state=state,
        city=city,
        is_active=active,
        certified_bed_count=beds,
        provider_subtype="Short-term",
        source="test",
        ingested_at=NOW,
    )


def _source(source_file, name, state, count=100, ccn=None):
    return dict(
        source_file=source_file,
        hospital_name=name,
        license_state=state,
        charge_count=count,
        ccn=ccn,
        ingested_at=NOW,
    )


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'gap.sqlite'}")
    init_db(eng)
    return eng


# --- brand inference ------------------------------------------------------


def test_system_key_finds_the_brand():
    assert system_key("Baylor Scott & White Medical Center - Temple") == "BAYLOR"
    assert system_key("CHRISTUS Hospital - Orange") == "CHRISTUS"
    assert system_key("Ascension Via Christi Hospital") == "ASCENSION"


def test_saint_names_need_a_second_token():
    """'Saint' alone would merge unrelated systems."""

    assert system_key("Saint Lukes Hospital") == "SAINT LUKES"
    assert system_key("St. Francis Hospital Inc") == "ST FRANCIS"
    assert system_key("Saint Lukes Hospital") != system_key("St Francis Hospital")


def test_a_purely_descriptive_name_has_no_brand():
    assert system_key("County Hospital") is None
    assert system_key("") is None
    assert system_key(None) is None


def test_a_leading_descriptor_falls_through_to_the_real_name():
    assert system_key("Community Hospital of Bremen") == "BREMEN"


# --- the report -----------------------------------------------------------


def test_downloaded_hospitals_drop_off_the_worklist(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                _hospital("170001", "Sunflower General Hospital", "KS"),
                _hospital("170002", "Prairie Community Hospital", "KS"),
            ],
        )
        # Linked by CCN.
        conn.execute(
            insert(charge_sources),
            [_source("a.csv", "Sunflower General Hospital", "KS", ccn="170001")],
        )

    report = build_gap_report(engine)
    assert report.total_covered == 1
    assert report.total_remaining == 1
    assert [g.name for g in report.independents] == ["Prairie Community Hospital"]


def test_an_unlinked_source_still_counts_by_name_and_state(engine):
    """Coverage must not depend on having run the crosswalk linker."""

    with engine.begin() as conn:
        conn.execute(insert(hospitals), [_hospital("170001", "Sunflower General", "KS")])
        conn.execute(insert(charge_sources), [_source("a.csv", "Sunflower General", "KS")])

    report = build_gap_report(engine)
    assert report.total_covered == 1
    assert report.independents == []


def test_a_zero_row_source_does_not_count_as_covered(engine):
    with engine.begin() as conn:
        conn.execute(insert(hospitals), [_hospital("170001", "Sunflower General", "KS")])
        conn.execute(
            insert(charge_sources),
            [_source("a.csv", "Sunflower General", "KS", count=0)],
        )

    report = build_gap_report(engine)
    assert report.total_covered == 0


def test_systems_outrank_independents_and_known_publishers_come_first(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                # A big system we have never pulled from.
                _hospital("450001", "Ascension Providence Hospital", "TX"),
                _hospital("450002", "Ascension Seton Medical Center", "TX"),
                _hospital("450003", "Ascension Via Christi Hospital", "KS"),
                # A smaller system we already have one file from.
                _hospital("450004", "Baylor Scott & White - Plano", "TX"),
                _hospital("450005", "Baylor Scott & White - Waco", "TX"),
                # A true independent.
                _hospital("450006", "Prairie Community Hospital", "KS"),
            ],
        )
        conn.execute(
            insert(charge_sources),
            [_source("bsw_temple.csv", "Baylor Scott & White - Temple", "TX")],
        )

    report = build_gap_report(engine)

    # Known publisher ranks first even though Ascension has more hospitals.
    assert [s.system for s in report.systems] == ["BAYLOR", "ASCENSION"]
    assert report.systems[0].already_publishing is True
    assert report.systems[0].known_source == "bsw_temple.csv"
    assert report.systems[1].already_publishing is False
    assert report.systems[1].remaining == 3
    assert report.systems[1].states == ["KS", "TX"]

    assert [g.name for g in report.independents] == ["Prairie Community Hospital"]


def test_min_system_size_moves_small_clusters_to_independents(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                _hospital("450001", "Ascension Providence Hospital", "TX"),
                _hospital("450002", "Ascension Seton Medical Center", "TX"),
            ],
        )

    assert build_gap_report(engine, min_system_size=2).systems
    big = build_gap_report(engine, min_system_size=3)
    assert big.systems == []
    assert len(big.independents) == 2


def test_inactive_hospitals_are_not_in_the_universe(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                _hospital("170001", "Sunflower General", "KS"),
                _hospital("170002", "Closed Memorial", "KS", active=False),
            ],
        )

    report = build_gap_report(engine)
    assert report.total_hospitals == 1


# --- coverage + uncrawled states -----------------------------------------


def test_state_coverage_and_untouched_states(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                _hospital("170001", "Sunflower General", "KS"),
                _hospital("170002", "Prairie Community", "KS"),
                _hospital("210001", "Chesapeake Regional", "MD"),
            ],
        )
        conn.execute(insert(charge_sources), [_source("a.csv", "Sunflower General", "KS")])

    report = build_gap_report(engine)
    by_state = {c.state: c for c in report.coverage}
    assert by_state["KS"].total == 2
    assert by_state["KS"].covered == 1
    assert by_state["KS"].pct == 50.0
    assert by_state["MD"].remaining == 1
    assert report.uncrawled_states == ["MD"]


# --- workbook -------------------------------------------------------------


def test_write_xlsx_has_every_sheet(engine, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")

    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                _hospital("450001", "Ascension Providence Hospital", "TX"),
                _hospital("450002", "Ascension Seton Medical Center", "TX"),
                _hospital("170001", "Prairie Community Hospital", "KS"),
            ],
        )

    out = str(tmp_path / "worklist.xlsx")
    write_xlsx(build_gap_report(engine), out)
    assert os.path.exists(out)

    book = openpyxl.load_workbook(out)
    assert book.sheetnames == [
        "Priority Systems",
        "Independents by State",
        "State Coverage",
        "Unattributed Files",
        "Probably Already Held",
        "How to find a file",
    ]

    systems = book["Priority Systems"]
    assert systems["B2"].value == "Ascension"
    assert systems["C2"].value == 2

    independents = book["Independents by State"]
    assert independents["A2"].value == "KS"
    assert independents["C2"].value == "Prairie Community Hospital"

    coverage = book["State Coverage"]
    assert {coverage.cell(row=r, column=6).value for r in (2, 3)} == {"not started"}


# --- files we hold but cannot attribute -----------------------------------


def test_a_file_with_no_identifiers_is_reported_not_counted_either_way(engine):
    """We have the data, but nothing to join it to — that must be visible.

    Silently counting it as covered would hide a hospital we cannot query;
    silently counting it as a gap would have someone download it twice.
    """

    with engine.begin() as conn:
        conn.execute(insert(hospitals), [_hospital("040055", "National Park Medical Center", "AR")])
        conn.execute(
            insert(charge_sources),
            [
                dict(
                    source_file="national-park-13200.csv",
                    hospital_name=None,
                    license_state=None,
                    hospital_address=None,
                    ein=None,
                    primary_npi=None,
                    charge_count=28005,
                    ingested_at=NOW,
                )
            ],
        )

    report = build_gap_report(engine)
    assert report.total_covered == 0
    assert [u.source_file for u in report.unattributed] == ["national-park-13200.csv"]
    assert report.unattributed[0].charge_count == 28005


def test_attributed_sources_stay_out_of_the_unattributed_list(engine):
    with engine.begin() as conn:
        conn.execute(insert(hospitals), [_hospital("170001", "Sunflower General", "KS")])
        conn.execute(insert(charge_sources), [_source("a.csv", "Sunflower General", "KS")])

    assert build_gap_report(engine).unattributed == []


def test_unattributed_files_are_listed_biggest_first(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(charge_sources),
            [
                _source("small.csv", None, None, count=100),
                _source("big.csv", None, None, count=50_000),
            ],
        )

    report = build_gap_report(engine)
    assert [u.source_file for u in report.unattributed] == ["big.csv", "small.csv"]


# --- finding the file to download -----------------------------------------


def test_the_search_url_pins_the_hospital_and_the_right_words():
    from hospitals.gap import mrf_search_url

    url = mrf_search_url("Pratt Regional Medical Center", "Pratt", "KS")

    assert url.startswith("https://www.google.com/search?q=")
    assert "%22Pratt+Regional+Medical+Center%22" in url  # quoted, so it pins
    assert "Pratt" in url and "KS" in url
    assert "machine+readable" in url
    assert "standard+charges" in url


def test_a_name_with_punctuation_is_escaped():
    from hospitals.gap import mrf_search_url

    url = mrf_search_url("HCA HealthOne Presbyterian St Luke\'s", state="CO")
    assert " " not in url
    assert "Luke%27s" in url


def test_city_and_state_are_optional():
    from hospitals.gap import mrf_search_url

    assert "%22Ochsner+Health%22" in mrf_search_url("Ochsner Health")


def _seed_gaps(engine):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            [
                _hospital("450001", "Ascension Providence Hospital", "TX"),
                _hospital("450002", "Ascension Seton Medical Center", "TX"),
                _hospital("170001", "Prairie Community Hospital", "KS"),
            ],
        )


def test_the_workbook_carries_a_clickable_link_on_both_worklists(engine, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    _seed_gaps(engine)

    out = str(tmp_path / "gaps.xlsx")
    write_xlsx(build_gap_report(engine), out)
    book = openpyxl.load_workbook(out)

    systems = book["Priority Systems"]
    assert systems.cell(row=1, column=8).value == "Find MRF"
    assert systems.cell(row=2, column=8).value == "Find MRF"
    assert systems.cell(row=2, column=8).hyperlink.target.startswith("https://")

    independents = book["Independents by State"]
    assert independents.cell(row=1, column=7).value == "Find MRF"
    assert independents.cell(row=2, column=7).hyperlink.target.startswith("https://")


def test_the_workbook_explains_the_cms_index_path(engine, tmp_path):
    """The domain shortcut is worth more than the search once you know it."""

    openpyxl = pytest.importorskip("openpyxl")
    _seed_gaps(engine)

    out = str(tmp_path / "gaps.xlsx")
    write_xlsx(build_gap_report(engine), out)

    book = openpyxl.load_workbook(out)
    assert "How to find a file" in book.sheetnames
    text = " ".join(
        str(c.value)
        for row in book["How to find a file"].iter_rows()
        for c in row
        if c.value
    )
    assert "cms-hpt.txt" in text


# --- the file we already have -----------------------------------------------


def _put(engine, hospital_rows=(), source_rows=()):
    with engine.begin() as conn:
        if hospital_rows:
            conn.execute(insert(hospitals), list(hospital_rows))
        if source_rows:
            conn.execute(insert(charge_sources), list(source_rows))


def test_a_held_file_is_offered_next_to_the_gap_it_resembles(engine):
    """"Are we really missing Mercy?" turned out to mean four things at once.

    Some Mercys were genuinely absent, some were covered under a sibling's EIN,
    and some sat in the database unlinked because nothing could join on them.
    Only the last is fixable without downloading anything, and it is invisible
    in a list of gaps: the hospital looks missing, the file looks orphaned, and
    nothing puts the two side by side.
    """

    _put(
        engine,
        [_hospital("351320", "Mercy Medical Center Williston", "ND")],
        [_source("CNDMW_Mercy Medical Center - Williston_2025.07.01_CDM.xlsx",
                 None, None, count=9702)],
    )

    report = build_gap_report(engine)
    assert len(report.probable) == 1
    assert report.probable[0].ccn == "351320"
    assert report.probable[0].charge_count == 9702
    assert report.probable[0].score >= 0.3


def test_a_file_that_resembles_nothing_is_not_paired_with_anything(engine):
    _put(
        engine,
        [_hospital("170027", "Pratt Regional Medical Center", "KS")],
        [_source("99-9999999_totally-unrelated-clinic.json", "Unrelated Clinic", None,
                 count=50)],
    )

    assert build_gap_report(engine).probable == []


def test_the_filename_is_searched_as_well_as_the_name_inside(engine):
    """A system file names the system in its metadata and the facility in its
    filename, so matching on one alone finds neither."""

    _put(
        engine,
        [_hospital("060112", "HCA HealthONE Sky Ridge", "CO")],
        [_source("84-1321373_HCA-HEALTHONE-SKY-RIDGE_standardcharges.json",
                 "HealthONE", None, count=3_026_183)],
    )

    assert build_gap_report(engine).probable[0].ccn == "060112"


def test_a_covered_hospital_is_never_offered_a_second_file(engine):
    _put(
        engine,
        [_hospital("351320", "Mercy Medical Center Williston", "ND")],
        [
            _source("linked.json", "Mercy Medical Center Williston", "ND",
                    ccn="351320", count=100),
            _source("orphan_mercy-medical-center-williston.xlsx", None, None, count=9702),
        ],
    )

    assert build_gap_report(engine).probable == []


def test_recoverable_rows_counts_each_file_once(engine):
    """One orphan may resemble several gaps; its rows are not multiplied."""

    _put(
        engine,
        [
            _hospital("351320", "Mercy Medical Center Williston", "ND"),
            _hospital("351321", "Mercy Medical Center Williston North", "ND"),
        ],
        [_source("mercy-medical-center-williston.xlsx", None, None, count=9702)],
    )

    report = build_gap_report(engine)
    assert len(report.probable) == 2
    assert report.recoverable_rows == 9702


def test_the_biggest_recoverable_files_come_first(engine):
    _put(
        engine,
        [
            _hospital("351320", "Mercy Medical Center Williston", "ND"),
            _hospital("060112", "HCA HealthONE Sky Ridge", "CO"),
        ],
        [
            _source("mercy-medical-center-williston.xlsx", None, None, count=9702),
            _source("hca-healthone-sky-ridge.json", None, None, count=3_026_183),
        ],
    )

    assert build_gap_report(engine).probable[0].ccn == "060112"


def test_a_hospital_in_another_state_is_never_offered(engine):
    """The sheet proposed an Arkansas rehab hospital for a Houston file, and
    St. Francis hospitals in Delaware, Illinois and Minnesota for one published
    in New York. Every one scored well. Every one was wrong.

    A hospital's name is not unique; its name and state very nearly are.
    """

    _put(
        engine,
        [
            _hospital("080003", "St Francis Hospital", "DE"),
            _hospital("141350", "St Francis Hospital", "IL"),
            _hospital("330404", "St Francis Hospital and Heart Center", "NY"),
        ],
        [_source("112050523_st-francis-hospital-heart-center.json",
                 "St. Francis Hospital", "NY", count=3_859_630)],
    )

    matches = build_gap_report(engine).probable

    assert {m.state for m in matches} == {"NY"}
    assert matches[0].ccn == "330404"
    assert matches[0].same_state is True


def test_a_source_with_no_state_still_gets_candidates(engine):
    """There is nothing else to go on — but the reviewer is told which kind of
    guess they are looking at."""

    _put(
        engine,
        [_hospital("080003", "St Francis Hospital", "DE")],
        [_source("st-francis.json", "St. Francis Hospital", None, count=100)],
    )

    matches = build_gap_report(engine).probable
    assert len(matches) == 1
    assert matches[0].same_state is False


def test_a_freestanding_er_no_longer_drags_in_three_states(engine):
    """84-1321373_HCA-HEALTHONE-SOUTHWEST-ER proposed Southwest hospitals in
    Colorado, Kansas and Mississippi. It is a Colorado ER with no CCN at all."""

    _put(
        engine,
        [
            _hospital("061327", "Southwest Memorial Hospital", "CO"),
            _hospital("170068", "Southwest Medical Center", "KS"),
            _hospital("250097", "Southwest MS Regional Medical Center", "MS"),
        ],
        [_source("84-1321373_HCA-HEALTHONE-SOUTHWEST-ER.json",
                 "HCA HEALTHONE SOUTHWEST ER", "CO", count=2_997_164)],
    )

    assert {m.state for m in build_gap_report(engine).probable} == {"CO"}
