"""Gzipped MRFs, and never dropping a file without saying so.

Large systems publish multi-gigabyte exports gzipped. Two things had to be
true before a batch of them could be trusted: the parser has to read them
without unpacking to disk, and any file the ingester cannot read has to be
named rather than silently passed over — which is how a half-finished
``.crdownload`` cost us a hospital in an earlier batch.
"""

import gzip
import os
import shutil

import pytest

from hospitals import price_transparency as pt
from hospitals.ingest_charges import SUPPORTED, ingest_charge_path

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
TALL = os.path.join(FIX, "mrf_tall_sample.csv")
JSON = os.path.join(FIX, "mrf_sample.json")


def gzipped(src, dest):
    with open(src, "rb") as fh, gzip.open(dest, "wb") as out:
        shutil.copyfileobj(fh, out)
    return str(dest)


# --- reading --------------------------------------------------------------


def test_a_gzipped_csv_reads_like_the_plain_one(tmp_path):
    plain_meta, plain_rows = pt.read_any(TALL)
    plain_rows = list(plain_rows)

    gz = gzipped(TALL, tmp_path / "170001_sunflower_standardcharges.csv.gz")
    meta, rows = pt.read_any(gz)
    rows = list(rows)

    assert meta.layout == plain_meta.layout == "tall"
    assert meta.hospital_name == plain_meta.hospital_name
    assert len(rows) == len(plain_rows) == 3
    assert [r.description for r in rows] == [r.description for r in plain_rows]
    assert [r.negotiated_dollar for r in rows] == [r.negotiated_dollar for r in plain_rows]


def test_a_gzipped_json_reads_like_the_plain_one(tmp_path):
    plain_meta, plain_rows = pt.read_any(JSON)
    plain_rows = list(plain_rows)

    gz = gzipped(JSON, tmp_path / "170002_cascade_standardcharges.json.gz")
    meta, rows = pt.read_any(gz)
    rows = list(rows)

    assert meta.layout == "json"
    assert meta.hospital_name == plain_meta.hospital_name
    assert len(rows) == len(plain_rows)


def test_the_ein_still_comes_off_a_gz_filename(tmp_path):
    gz = gzipped(TALL, tmp_path / "480543747_pratt-regional_standardcharges.csv.gz")
    meta, _ = pt.read_any(gz)
    assert meta.ein == "480543747"
    assert meta.source_file.endswith(".csv.gz")


def test_a_gzipped_cp1252_file_still_detects_its_encoding(tmp_path):
    gz = gzipped(
        os.path.join(FIX, "mrf_cp1252_sample.csv"),
        tmp_path / "170003_children_standardcharges.csv.gz",
    )
    _, rows = pt.read_any(gz)
    assert list(rows)[0].description == "Children’s CT scan head"


# --- nothing is dropped in silence ----------------------------------------


def test_unsupported_files_are_reported_not_silently_skipped(tmp_path, caplog):
    folder = tmp_path / "round6"
    folder.mkdir()
    shutil.copy(TALL, folder / "111111111_good_standardcharges.csv")
    (folder / "Unconfirmed 633144.crdownload").write_text("half a download")
    (folder / "notes.rtf").write_text("not an MRF")

    db_url = f"sqlite:///{tmp_path / 'r6.sqlite'}"
    with caplog.at_level("WARNING"):
        summaries = ingest_charge_path(str(folder), database_url=db_url)

    assert len(summaries) == 1
    logged = caplog.text
    assert "Unconfirmed 633144.crdownload" in logged
    assert "notes.rtf" in logged
    assert "never read" in logged


def test_a_gz_in_the_folder_is_picked_up_by_a_batch(tmp_path):
    folder = tmp_path / "round6"
    folder.mkdir()
    shutil.copy(TALL, folder / "111111111_plain_standardcharges.csv")
    gzipped(TALL, folder / "222222222_zipped_standardcharges.csv.gz")

    db_url = f"sqlite:///{tmp_path / 'r6.sqlite'}"
    summaries = ingest_charge_path(str(folder), database_url=db_url)

    assert sorted(s.ein for s in summaries) == ["111111111", "222222222"]
    assert all(s.charges_loaded == 3 for s in summaries)


def test_a_subdirectory_is_reported_but_not_as_a_bad_file(tmp_path, caplog):
    """A folder is skipped by design, but it is still named.

    It must not be counted among the unreadable files either — a sibling round
    folder is a deliberate skip, not a defect.
    """

    folder = tmp_path / "round6"
    folder.mkdir()
    (folder / "nested").mkdir()
    shutil.copy(TALL, folder / "111111111_good_standardcharges.csv")

    db_url = f"sqlite:///{tmp_path / 'r6.sqlite'}"
    with caplog.at_level("WARNING"):
        ingest_charge_path(str(folder), database_url=db_url)

    assert "nested" in caplog.text
    assert "Not descending" in caplog.text
    assert "unsupported type" not in caplog.text


def test_gz_is_in_the_supported_list():
    assert ".gz" in SUPPORTED


def test_a_folder_of_only_unreadable_files_raises(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "a.crdownload").write_text("x")
    with pytest.raises(FileNotFoundError, match="no .*files in directory"):
        ingest_charge_path(str(folder), database_url=f"sqlite:///{tmp_path / 'x.sqlite'}")


# --- subdirectories -------------------------------------------------------


def test_a_skipped_subdirectory_is_named(tmp_path, caplog):
    """A system's folder of facilities must not vanish without a word.

    Round 6 arrived as a flat set of files plus an "HCA" folder holding the
    system's facilities. Skipping it silently would have lost ~180 hospitals
    and looked like a clean run.
    """

    folder = tmp_path / "round6"
    (folder / "HCA").mkdir(parents=True)
    shutil.copy(TALL, folder / "111111111_flat_standardcharges.csv")
    shutil.copy(TALL, folder / "HCA" / "222222222_hca_standardcharges.csv")

    db_url = f"sqlite:///{tmp_path / 'r6.sqlite'}"
    with caplog.at_level("WARNING"):
        summaries = ingest_charge_path(str(folder), database_url=db_url)

    assert [s.ein for s in summaries] == ["111111111"]
    assert "HCA" in caplog.text
    assert "--recursive" in caplog.text
    assert "never entered" in caplog.text


def test_recursive_reaches_a_systems_folder(tmp_path):
    folder = tmp_path / "round6"
    (folder / "HCA").mkdir(parents=True)
    shutil.copy(TALL, folder / "111111111_flat_standardcharges.csv")
    shutil.copy(TALL, folder / "HCA" / "222222222_hca_standardcharges.csv")
    shutil.copy(TALL, folder / "HCA" / "333333333_hca_two_standardcharges.csv")

    db_url = f"sqlite:///{tmp_path / 'r6.sqlite'}"
    summaries = ingest_charge_path(str(folder), database_url=db_url, recursive=True)

    assert sorted(s.ein for s in summaries) == ["111111111", "222222222", "333333333"]
    assert all(s.charges_loaded == 3 for s in summaries)


def test_recursive_still_reports_unsupported_files_from_below(tmp_path, caplog):
    folder = tmp_path / "round6"
    (folder / "HCA").mkdir(parents=True)
    shutil.copy(TALL, folder / "HCA" / "222222222_hca_standardcharges.csv")
    (folder / "HCA" / "Unconfirmed 900001.crdownload").write_text("partial")

    db_url = f"sqlite:///{tmp_path / 'r6.sqlite'}"
    with caplog.at_level("WARNING"):
        ingest_charge_path(str(folder), database_url=db_url, recursive=True)
    assert "Unconfirmed 900001.crdownload" in caplog.text
