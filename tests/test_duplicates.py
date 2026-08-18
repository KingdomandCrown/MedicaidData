"""Detecting the same dataset stored more than once.

Round 6 surfaced two distinct habits. A download folder holds ``file.csv`` and
``file (1).csv``. And a large system publishes one file per EIN, shipping a
copy named for each facility — HCA Houston Southeast and HCA Houston
Rehabilitation Southeast both loaded 9,582,017 rows, to the digit.

Telling them apart matters because only the first is safe to delete.
"""

import datetime as dt

import pytest
from sqlalchemy import insert, select

from hospitals.db import charge_sources, init_db, make_engine, standard_charges
from hospitals.duplicates import (
    canonical_name,
    find_duplicate_loads,
    prune_redownloads,
)

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


# --- grouping -------------------------------------------------------------


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
    assert group.redownload_files == ["akron_standardcharges (1).csv"]


def test_one_dataset_named_per_facility_is_caught_and_distinguished(engine):
    load(engine, [
        _source("southeast.json", "621801359", 9582017, "HCA HOUSTON SOUTHEAST"),
        _source("rehab-southeast.json", "621801359", 9582017, "HCA Houston Rehab Southeast"),
    ])

    group = find_duplicate_loads(engine).groups[0]
    assert group.redundant_rows == 9582017
    # Two different facility names on one dataset — not a re-download.
    assert group.looks_like_one_file_per_facility is True
    assert group.per_facility_datasets == 2
    assert group.redownload_files == []


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


# --- telling the two habits apart -----------------------------------------


def test_canonical_name_ignores_a_download_counter_and_the_extension():
    assert canonical_name("anmed (1).zip") == canonical_name("anmed.zip")
    assert canonical_name("a/b/anmed (12).zip") == "anmed"
    assert canonical_name("anmed.csv.gz") == "anmed"


def test_a_trailing_number_that_is_not_a_counter_is_left_alone():
    """Rows get deleted on the strength of this match, so it stays narrow.

    ``charges_2024`` and ``charges_2023`` are different files. Collapsing them
    would delete a year of data to save disk.
    """

    assert canonical_name("charges_2024.csv") != canonical_name("charges_2023.csv")
    assert canonical_name("campus-2.csv") != canonical_name("campus.csv")


def test_the_facility_split_reads_filenames_not_the_recorded_hospital_name(engine):
    """Cone Health ships four facilities' files that all name the system.

    Judging by ``hospital_name`` alone, this looks like one hospital downloaded
    four times — and three files would be deleted. The filenames say otherwise.
    """

    load(engine, [
        _source("581588823_TheMosesHConeMemorialHospital_standardcharges.csv", "581588823", 1419541, "Cone Health"),
        _source("581588823_anniepennhospital_standardcharges.csv", "581588823", 1419541, "Cone Health"),
        _source("581588823_conehealthbehavioralhealthhospital_standardcharges.csv", "581588823", 1419541, "Cone Health"),
        _source("581588823_wesleylonghospital_standardcharges.csv", "581588823", 1419541, "Cone Health"),
    ])

    group = find_duplicate_loads(engine).groups[0]
    assert group.distinct_names == 1            # what the files say inside
    assert group.per_facility_datasets == 4     # what the filenames say
    assert group.looks_like_one_file_per_facility is True
    assert group.redownload_files == []         # nothing safe to delete here


def test_a_re_download_inside_a_per_facility_group_is_still_found(engine):
    """HealthOne's real shape: two facilities, one of them downloaded twice."""

    load(engine, [
        _source("84-1321373_HCA-HEALTHONE-SKY-RIDGE_standardcharges.json", "841321373", 3026183, "HealthOne"),
        _source("84-1321373_HCA-HEALTHONE-SOUTH-PARKER-ER_standardcharges.json", "841321373", 3026183, "HealthOne"),
        _source("84-1321373_HCA-HEALTHONE-SOUTH-PARKER-ER_standardcharges (1).json", "841321373", 3026183, "HealthOne"),
    ])

    group = find_duplicate_loads(engine).groups[0]
    assert group.per_facility_datasets == 2
    assert group.redownload_files == [
        "84-1321373_HCA-HEALTHONE-SOUTH-PARKER-ER_standardcharges (1).json"
    ]
    assert group.redownload_rows == 3026183
    assert group.per_facility_rows == 3026183
    assert group.redundant_rows == group.redownload_rows + group.per_facility_rows


def test_the_report_splits_deletable_rows_from_the_rest(engine):
    load(engine, [
        _source("dup.csv", "111111111", 1_000_000, "A"),
        _source("dup (1).csv", "111111111", 1_000_000, "A"),
        _source("north.json", "222222222", 1_500_000, "System"),
        _source("south.json", "222222222", 1_500_000, "System"),
    ])

    report = find_duplicate_loads(engine)
    assert report.redownload_rows == 1_000_000
    assert report.per_facility_rows == 1_500_000
    assert report.redundant_rows == 2_500_000
    assert report.redownload_share == 20.0
    assert report.per_facility_share == 30.0


# --- pruning --------------------------------------------------------------


def _with_charges(engine, spec):
    """Insert sources and give each one its charge rows, so deletes are visible."""

    with engine.begin() as conn:
        for source_file, ein, count, name in spec:
            result = conn.execute(
                insert(charge_sources),
                _source(source_file, ein, count, name),
            )
            source_id = int(result.inserted_primary_key[0])
            conn.execute(
                insert(standard_charges),
                [{"source_id": source_id, "ein": ein, "code": str(i)} for i in range(count)],
            )


def _remaining(engine):
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(select(charge_sources.c.source_file))}


def test_a_dry_run_deletes_nothing(engine):
    _with_charges(engine, [
        ("akron.csv", "340714357", 3, "Akron"),
        ("akron (1).csv", "340714357", 3, "Akron"),
    ])

    summary = prune_redownloads(engine)
    assert summary.applied is False
    assert summary.files == ["akron (1).csv"]
    assert summary.rows == 3
    assert _remaining(engine) == {"akron.csv", "akron (1).csv"}


def test_applying_removes_the_copy_and_its_charge_rows(engine):
    _with_charges(engine, [
        ("akron.csv", "340714357", 3, "Akron"),
        ("akron (1).csv", "340714357", 3, "Akron"),
    ])

    summary = prune_redownloads(engine, apply=True)
    assert summary.applied is True
    assert summary.file_count == 1
    assert _remaining(engine) == {"akron.csv"}

    with engine.connect() as conn:
        left = conn.execute(select(standard_charges.c.id)).all()
    assert len(left) == 3  # only the surviving copy's rows


def test_the_copy_without_a_counter_is_the_one_kept(engine):
    _with_charges(engine, [
        ("anmed (1).zip", "570359174", 2, "AnMed"),
        ("anmed (2).zip", "570359174", 2, "AnMed"),
        ("anmed (3).zip", "570359174", 2, "AnMed"),
        ("anmed.zip", "570359174", 2, "AnMed"),
    ])

    prune_redownloads(engine, apply=True)
    assert _remaining(engine) == {"anmed.zip"}


def test_pruning_never_touches_a_per_facility_copy(engine):
    _with_charges(engine, [
        ("581588823_anniepennhospital.csv", "581588823", 2, "Cone Health"),
        ("581588823_wesleylonghospital.csv", "581588823", 2, "Cone Health"),
    ])

    summary = prune_redownloads(engine, apply=True)
    assert summary.file_count == 0
    assert len(_remaining(engine)) == 2


def test_pruning_is_idempotent(engine):
    _with_charges(engine, [
        ("akron.csv", "340714357", 2, "Akron"),
        ("akron (1).csv", "340714357", 2, "Akron"),
    ])

    prune_redownloads(engine, apply=True)
    again = prune_redownloads(engine, apply=True)
    assert again.file_count == 0
    assert _remaining(engine) == {"akron.csv"}


def test_a_batched_prune_removes_every_copy(engine):
    _with_charges(
        engine,
        [(f"file{n}.csv", "111111111", 1, "A") for n in range(3)]
        + [(f"file{n} (1).csv", "111111111", 1, "A") for n in range(3)],
    )

    summary = prune_redownloads(engine, apply=True, batch_size=2)
    assert summary.file_count == 3
    assert _remaining(engine) == {"file0.csv", "file1.csv", "file2.csv"}


# --- files that have no EIN to group by -----------------------------------


def _npi_source(source_file, npi, count, name=None):
    return dict(
        source_file=source_file,
        ein=None,
        primary_npi=npi,
        charge_count=count,
        hospital_name=name,
        ingested_at=NOW,
    )


def test_files_with_no_ein_are_grouped_by_npi(engine):
    """Atrium publishes under its NPI, so repair-eins leaves its EIN null.

    Grouping on the EIN alone would stop checking the largest duplicates in
    the store the moment their invented EINs were corrected.
    """

    load(engine, [
        _npi_source("1669348991_atrium_standardcharges.csv", "1669348991", 8544927, "Atrium"),
        _npi_source("1669348991_atrium_standardcharges (1).csv", "1669348991", 8544927, "Atrium"),
    ])

    report = find_duplicate_loads(engine)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.ein is None
    assert group.npi == "1669348991"
    assert group.entity == "NPI 1669348991"
    assert group.redownload_files == ["1669348991_atrium_standardcharges (1).csv"]


def test_a_group_with_an_ein_still_displays_the_ein(engine):
    load(engine, [
        _source("akron.csv", "340714357", 5, "Akron"),
        _source("akron (1).csv", "340714357", 5, "Akron"),
    ])
    assert find_duplicate_loads(engine).groups[0].entity == "EIN 340714357"


def test_different_npis_are_not_grouped(engine):
    load(engine, [
        _npi_source("a.csv", "1669348991", 500000, "A"),
        _npi_source("b.csv", "1730055054", 500000, "B"),
    ])
    assert find_duplicate_loads(engine).groups == []


def test_a_file_with_neither_ein_nor_npi_is_still_never_grouped(engine):
    load(engine, [
        _npi_source("x.csv", None, 500000, "X"),
        _npi_source("y.csv", None, 500000, "Y"),
    ])
    assert find_duplicate_loads(engine).groups == []


def test_pruning_works_on_an_npi_keyed_group(engine):
    with engine.begin() as conn:
        conn.execute(insert(charge_sources), [
            _npi_source("atrium.csv", "1669348991", 2, "Atrium"),
            _npi_source("atrium (1).csv", "1669348991", 2, "Atrium"),
        ])

    summary = prune_redownloads(engine, apply=True)
    assert summary.files == ["atrium (1).csv"]
    assert _remaining(engine) == {"atrium.csv"}
