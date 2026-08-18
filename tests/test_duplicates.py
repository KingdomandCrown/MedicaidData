"""Detecting the same dataset stored more than once.

Round 6 surfaced two distinct habits. A download folder holds ``file.csv`` and
``file (1).csv``. And a large system publishes one file per EIN, shipping a
copy named for each facility — HCA Houston Southeast and HCA Houston
Rehabilitation Southeast both loaded 9,582,017 rows, to the digit.
"""

import datetime as dt

import pytest
from sqlalchemy import insert

from hospitals.db import charge_sources, init_db, make_engine
from hospitals.duplicates import find_duplicate_loads

NOW = dt.datetime(2026, 8, 18)


def _source(source_file, ein, count, name=None):
    return dict(
        source_file=source_file,
        ein=ein,
        charge_count=count,
        hospital_name=name,
        ingested_at=NOW,
    )


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'd.sqlite'}")
    init_db(eng)
    return eng


def load(engine, rows):
    with engine.begin() as conn:
        conn.execute(insert(charge_sources), rows)


def test_a_re_download_is_caught(engine):
    load(engine, [
        _source("akron_standardcharges.csv", "340714357", 2830769, "Akron"),
        _source("akron_standardcharges (1).csv", "340714357", 2830769, "Akron"),
    ])

    report = find_duplicate_loads(engine)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.copies == 2
    assert group.redundant_rows == 2830769
    assert group.looks_like_one_file_per_facility is False


def test_one_dataset_named_per_facility_is_caught_and_distinguished(engine):
    load(engine, [
        _source("southeast.json", "621801359", 9582017, "HCA HOUSTON SOUTHEAST"),
        _source("rehab-southeast.json", "621801359", 9582017, "HCA Houston Rehab Southeast"),
    ])

    group = find_duplicate_loads(engine).groups[0]
    assert group.redundant_rows == 9582017
    # Two different facility names on one dataset — not a re-download.
    assert group.looks_like_one_file_per_facility is True
    assert group.distinct_names == 2


def test_different_row_counts_under_one_ein_are_not_duplicates(engine):
    # HCA Florida Largo and Largo West share an EIN but hold different data.
    load(engine, [
        _source("largo.json", "621026428", 2313824, "LARGO"),
        _source("largo-west.json", "621026428", 2635266, "LARGO WEST"),
    ])
    assert find_duplicate_loads(engine).groups == []


def test_the_same_count_under_different_eins_is_not_a_duplicate(engine):
    load(engine, [
        _source("a.json", "111111111", 500000, "A"),
        _source("b.json", "222222222", 500000, "B"),
    ])
    assert find_duplicate_loads(engine).groups == []


def test_sources_without_an_ein_are_never_grouped(engine):
    # Nothing ties them together, so a shared count proves nothing.
    load(engine, [
        _source("x.csv", None, 500000, "X"),
        _source("y.csv", None, 500000, "Y"),
    ])
    assert find_duplicate_loads(engine).groups == []


def test_zero_row_files_are_excluded(engine):
    load(engine, [
        _source("a.csv", "111111111", 0, "A"),
        _source("b.csv", "111111111", 0, "B"),
    ])
    assert find_duplicate_loads(engine).groups == []


def test_totals_and_share_are_reported(engine):
    load(engine, [
        _source("a.json", "841321373", 3_000_000, "Sky Ridge"),
        _source("b.json", "841321373", 3_000_000, "South Parker ER"),
        _source("c.json", "841321373", 3_000_000, "Swedish"),
        _source("solo.json", "999999999", 1_000_000, "Elsewhere"),
    ])

    report = find_duplicate_loads(engine)
    assert report.total_sources == 4
    assert report.total_rows == 10_000_000
    assert report.redundant_files == 2          # three copies, keep one
    assert report.redundant_rows == 6_000_000
    assert report.share_of_rows == 60.0


def test_groups_are_ordered_by_what_they_cost(engine):
    load(engine, [
        _source("small-a.json", "111111111", 100, "A"),
        _source("small-b.json", "111111111", 100, "A"),
        _source("big-a.json", "222222222", 9_000_000, "B"),
        _source("big-b.json", "222222222", 9_000_000, "B"),
    ])
    assert [g.ein for g in find_duplicate_loads(engine).groups] == ["222222222", "111111111"]


def test_an_empty_database_reports_nothing(engine):
    report = find_duplicate_loads(engine)
    assert report.groups == []
    assert report.share_of_rows == 0.0
