"""Tidying a download folder: only what the database says is loaded moves."""

import datetime as dt
import os

import pytest
from sqlalchemy import insert

from hospitals.archive import archive_ingested
from hospitals.db import charge_sources, init_db, make_engine

NOW = dt.datetime(2026, 8, 13)


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'a.sqlite'}"
    init_db(make_engine(url))
    return url


def _record(url, *files):
    """Mark files as loaded; a (name, count) pair sets the row count."""

    rows = []
    for f in files:
        name, count = f if isinstance(f, tuple) else (f, 100)
        rows.append(dict(source_file=name, charge_count=count, ingested_at=NOW))
    with make_engine(url).begin() as conn:
        conn.execute(insert(charge_sources), rows)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "round5"
    d.mkdir()
    for name in ("loaded.csv", "failed.csv", "notes.txt"):
        (d / name).write_text("x")
    return d


def test_a_dry_run_moves_nothing(db_url, folder, tmp_path):
    _record(db_url, "loaded.csv")
    dest = tmp_path / "_to_delete"

    summary = archive_ingested(str(folder), str(dest), database_url=db_url)

    assert summary.dry_run is True
    assert summary.moved == ["loaded.csv"]
    assert os.path.exists(folder / "loaded.csv")
    assert not dest.exists()


def test_apply_moves_only_the_loaded_file(db_url, folder, tmp_path):
    _record(db_url, "loaded.csv")
    dest = tmp_path / "_to_delete"

    summary = archive_ingested(str(folder), str(dest), database_url=db_url, apply=True)

    assert summary.moved == ["loaded.csv"]
    assert os.path.exists(dest / "loaded.csv")
    assert not os.path.exists(folder / "loaded.csv")
    # The file that never loaded stays where it is.
    assert summary.not_loaded == ["failed.csv"]
    assert os.path.exists(folder / "failed.csv")


def test_a_file_that_loaded_zero_rows_stays(db_url, folder, tmp_path):
    """Parsed-but-empty is not done — it is waiting on a parser fix."""

    _record(db_url, ("loaded.csv", 100), ("failed.csv", 0))

    summary = archive_ingested(
        str(folder), str(tmp_path / "out"), database_url=db_url, apply=True
    )
    assert summary.moved == ["loaded.csv"]
    assert summary.not_loaded == ["failed.csv"]
    assert os.path.exists(folder / "failed.csv")


def test_non_ingestible_files_are_left_alone(db_url, folder, tmp_path):
    _record(db_url, "loaded.csv")
    summary = archive_ingested(
        str(folder), str(tmp_path / "out"), database_url=db_url, apply=True
    )
    assert summary.other_files == ["notes.txt"]
    assert os.path.exists(folder / "notes.txt")


def test_an_existing_name_at_the_destination_is_never_overwritten(db_url, folder, tmp_path):
    _record(db_url, "loaded.csv")
    dest = tmp_path / "_to_delete"
    dest.mkdir()
    (dest / "loaded.csv").write_text("an earlier copy")

    summary = archive_ingested(str(folder), str(dest), database_url=db_url, apply=True)

    assert summary.moved == []
    assert summary.collisions == ["loaded.csv"]
    assert (dest / "loaded.csv").read_text() == "an earlier copy"
    assert os.path.exists(folder / "loaded.csv")


def test_an_upload_hash_prefix_still_matches(db_url, tmp_path):
    """The stored key has the hash stripped, so the on-disk name must be too."""

    folder = tmp_path / "round5"
    folder.mkdir()
    (folder / "a1b2c3d4-170001_sunflower_standardcharges.csv").write_text("x")
    _record(db_url, "170001_sunflower_standardcharges.csv")

    summary = archive_ingested(
        str(folder), str(tmp_path / "out"), database_url=db_url, apply=True
    )
    assert summary.moved == ["a1b2c3d4-170001_sunflower_standardcharges.csv"]


def test_subdirectories_are_not_touched(db_url, folder, tmp_path):
    (folder / "round6").mkdir()
    _record(db_url, "loaded.csv")

    summary = archive_ingested(
        str(folder), str(tmp_path / "out"), database_url=db_url, apply=True
    )
    assert "round6" not in summary.other_files
    assert os.path.isdir(folder / "round6")


def test_an_empty_database_moves_nothing(db_url, folder, tmp_path):
    summary = archive_ingested(
        str(folder), str(tmp_path / "out"), database_url=db_url, apply=True
    )
    assert summary.moved == []
    assert sorted(summary.not_loaded) == ["failed.csv", "loaded.csv"]


def test_a_missing_folder_raises(db_url, tmp_path):
    with pytest.raises(NotADirectoryError):
        archive_ingested(str(tmp_path / "nope"), str(tmp_path / "out"), database_url=db_url)
