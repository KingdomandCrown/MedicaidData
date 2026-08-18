"""Finding, across several folders, the MRF files the database already holds.

The question this answers is "what on this machine can go?", which
archive-ingested cannot: it tidies one named folder. Files pile up in
Downloads, on the Desktop, and inside a system's own subfolder, and the disk
they hold is the reason to look.
"""

import datetime as dt
import os

import pytest
from sqlalchemy import insert

from hospitals.db import charge_sources, init_db, make_engine
from hospitals.scan import human_bytes, scan_for_ingested, write_listing

NOW = dt.datetime(2026, 8, 18)


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 's.sqlite'}"
    init_db(make_engine(url))
    return url


def _record(url, *files):
    rows = [
        dict(source_file=f, charge_count=100, ingested_at=NOW)
        for f in files
    ]
    with make_engine(url).begin() as conn:
        conn.execute(insert(charge_sources), rows)


def _file(path, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return str(path)


# --- classifying ----------------------------------------------------------


def test_loaded_files_are_separated_from_the_ones_still_needed(tmp_path, db_url):
    desktop = tmp_path / "Desktop"
    _file(desktop / "done_standardcharges.csv", 500)
    _file(desktop / "todo_standardcharges.csv", 300)
    _record(db_url, "done_standardcharges.csv")

    summary = scan_for_ingested([str(desktop)], database_url=db_url)

    assert [f.size for f in summary.ingested] == [500]
    assert [f.size for f in summary.not_ingested] == [300]
    assert summary.reclaimable_bytes == 500
    assert summary.kept_bytes == 300


def test_several_roots_are_walked_in_one_pass(tmp_path, db_url):
    _file(tmp_path / "Desktop" / "a_standardcharges.csv", 100)
    _file(tmp_path / "Downloads" / "b_standardcharges.json", 200)
    _record(db_url, "a_standardcharges.csv", "b_standardcharges.json")

    summary = scan_for_ingested(
        [str(tmp_path / "Desktop"), str(tmp_path / "Downloads")], database_url=db_url
    )
    assert summary.reclaimable_bytes == 300


def test_subfolders_are_included(tmp_path, db_url):
    """A system's files arrive in their own folder — HCA shipped 335."""

    _file(tmp_path / "round6" / "HCA" / "hca_standardcharges.json", 900)
    _record(db_url, "hca_standardcharges.json")

    summary = scan_for_ingested([str(tmp_path / "round6")], database_url=db_url)
    assert len(summary.ingested) == 1
    assert summary.ingested[0].path.endswith(os.path.join("HCA", "hca_standardcharges.json"))


def test_files_that_are_not_mrfs_are_ignored(tmp_path, db_url):
    root = tmp_path / "Desktop"
    _file(root / "notes.txt", 100)
    _file(root / "screenshot.png", 100)
    _record(db_url, "notes.txt")

    summary = scan_for_ingested([str(root)], database_url=db_url)
    assert summary.ingested == []
    assert summary.not_ingested == []


def test_a_missing_root_is_reported_rather_than_fatal(tmp_path, db_url):
    real = tmp_path / "Desktop"
    _file(real / "a_standardcharges.csv")
    _record(db_url, "a_standardcharges.csv")

    summary = scan_for_ingested(
        [str(real), str(tmp_path / "nope")], database_url=db_url
    )
    assert summary.missing_roots == [str(tmp_path / "nope")]
    assert len(summary.ingested) == 1


def test_a_home_relative_path_is_expanded(tmp_path, db_url, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _file(tmp_path / "Desktop" / "a_standardcharges.csv", 42)
    _record(db_url, "a_standardcharges.csv")

    summary = scan_for_ingested(["~/Desktop"], database_url=db_url)
    assert summary.reclaimable_bytes == 42


# --- what the report is for -----------------------------------------------


def test_space_is_grouped_by_folder_so_you_know_where_to_look(tmp_path, db_url):
    _file(tmp_path / "Desktop" / "a_standardcharges.csv", 100)
    _file(tmp_path / "Downloads" / "b_standardcharges.csv", 900)
    _record(db_url, "a_standardcharges.csv", "b_standardcharges.csv")

    summary = scan_for_ingested([str(tmp_path)], database_url=db_url)
    by_dir = summary.by_directory

    assert by_dir[str(tmp_path / "Downloads")] == (1, 900)
    assert by_dir[str(tmp_path / "Desktop")] == (1, 100)


def test_the_biggest_files_come_first(tmp_path, db_url):
    root = tmp_path / "Desktop"
    _file(root / "small_standardcharges.csv", 10)
    _file(root / "big_standardcharges.csv", 5000)
    _record(db_url, "small_standardcharges.csv", "big_standardcharges.csv")

    summary = scan_for_ingested([str(root)], database_url=db_url)
    assert [f.size for f in summary.ingested] == [5000, 10]


def test_one_name_in_two_folders_is_flagged(tmp_path, db_url):
    """Both copies read as ingested; deleting both leaves no copy at all."""

    _file(tmp_path / "Desktop" / "a_standardcharges.csv", 100)
    _file(tmp_path / "Downloads" / "a_standardcharges.csv", 100)
    _record(db_url, "a_standardcharges.csv")

    summary = scan_for_ingested([str(tmp_path)], database_url=db_url)

    assert len(summary.ingested) == 2
    dupes = summary.duplicate_names
    assert list(dupes) == ["a_standardcharges.csv"]
    assert len(dupes["a_standardcharges.csv"]) == 2


def test_a_zero_row_file_is_not_treated_as_done(tmp_path, db_url):
    root = tmp_path / "Desktop"
    _file(root / "empty_standardcharges.csv", 100)
    with make_engine(db_url).begin() as conn:
        conn.execute(
            insert(charge_sources),
            dict(source_file="empty_standardcharges.csv", charge_count=0, ingested_at=NOW),
        )

    summary = scan_for_ingested([str(root)], database_url=db_url)
    assert summary.ingested == []
    assert len(summary.not_ingested) == 1


def test_the_listing_can_be_written_for_review(tmp_path, db_url):
    root = tmp_path / "Desktop"
    _file(root / "a_standardcharges.csv", 100)
    _record(db_url, "a_standardcharges.csv")

    summary = scan_for_ingested([str(root)], database_url=db_url)
    out = tmp_path / "to-delete.txt"
    assert write_listing(summary, str(out)) == 1
    assert out.read_text().strip().endswith("a_standardcharges.csv")


def test_nothing_on_disk_is_touched(tmp_path, db_url):
    root = tmp_path / "Desktop"
    path = _file(root / "a_standardcharges.csv", 100)
    _record(db_url, "a_standardcharges.csv")

    scan_for_ingested([str(root)], database_url=db_url)
    assert os.path.exists(path)


# --- readable sizes -------------------------------------------------------


@pytest.mark.parametrize(
    "size,expected",
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024**3, "5.0 GB")],
)
def test_sizes_read_as_sizes(size, expected):
    assert human_bytes(size) == expected
