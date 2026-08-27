"""Bringing the files down.

Two failures are worth guarding against specifically. A half-written MRF parses
— it simply stops early — so a hospital would look ingested while missing most
of its prices. And a runaway download fills a disk that has already been full
once in this project's life.
"""

import os

import pytest

from hospitals.mrf_fetch import (
    Fetched,
    extension_for,
    fetch_one,
    filename_for,
    slugify,
)

ROW = {"ccn": "170027", "name": "PRATT REGIONAL MEDICAL CENTER",
       "mrf_url": "https://prmc.org/charges.json"}


def _opener(chunks, content_type="application/json"):
    def opener(url):
        return content_type, iter(chunks)

    return opener


# --- the filename carries the answer --------------------------------------


def test_the_ccn_leads_the_filename():
    name = filename_for("170027", "PRATT REGIONAL MEDICAL CENTER",
                        "https://prmc.org/charges.json")
    assert name.startswith("ccn-170027_")
    assert name.endswith("_standardcharges.json")


def test_the_prefix_keeps_the_ccn_from_being_read_as_an_ein_or_npi():
    """Bare digits at the front of a filename are already claimed.

    ``ein_from_filename`` and ``npi_from_filename`` both parse a leading digit
    block, and a 6-digit CCN sitting there invites a wrong answer from whichever
    reaches it first.
    """

    from hospitals.price_transparency import ein_from_filename, npi_from_filename

    name = filename_for("170027", "PRATT", "https://x.org/a.json")
    assert ein_from_filename(name) is None
    assert npi_from_filename(name) is None


def test_a_punctuated_hospital_name_becomes_a_safe_slug():
    assert slugify("St. Luke's Hospital & Medical Center") == "st-luke-s-hospital-medical-center"


def test_a_nameless_hospital_still_gets_a_filename():
    assert filename_for("170027", "", "https://x.org/a.json").startswith("ccn-170027_hospital")


# --- what kind of file is this --------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.org/a.json", ".json"),
        ("https://x.org/a.csv", ".csv"),
        ("https://x.org/a.xlsx", ".xlsx"),
        ("https://x.org/a.zip", ".zip"),
        ("https://x.org/a.json.gz", ".json.gz"),
        ("https://x.org/a.csv.zip", ".csv.zip"),
        ("https://x.org/a.CSV?v=2", ".csv"),
        ("https://x.org/a%20b.json", ".json"),
    ],
)
def test_the_url_names_the_format(url, expected):
    assert extension_for(url) == expected


def test_the_content_type_is_used_only_when_the_url_says_nothing():
    assert extension_for("https://x.org/download", "application/json") == ".json"
    assert extension_for("https://x.org/download", "application/zip") == ".zip"


def test_a_json_file_served_as_plain_text_is_still_json():
    """Common enough that trusting the header would waste the download."""

    assert extension_for("https://x.org/a.json", "text/plain") == ".json"


def test_an_unrecognized_type_falls_back_to_csv():
    assert extension_for("https://x.org/download", "application/octet-stream") == ".csv"


# --- downloading ----------------------------------------------------------


def test_a_file_is_written_and_reported(tmp_path):
    result = fetch_one(ROW, str(tmp_path), opener=_opener([b"abc", b"def"]))

    assert result.status == "ok"
    assert result.bytes_written == 6
    assert os.path.basename(result.path).startswith("ccn-170027_")
    assert open(result.path, "rb").read() == b"abcdef"


def test_a_download_that_fails_part_way_leaves_nothing_behind(tmp_path):
    """A truncated MRF parses. It just stops early, and the hospital looks done."""

    def opener(url):
        def chunks():
            yield b"a" * 100
            raise ConnectionError("reset by peer")

        return "application/json", chunks()

    result = fetch_one(ROW, str(tmp_path), opener=opener)

    assert result.status == "error"
    assert "ConnectionError" in result.note
    assert list(tmp_path.iterdir()) == []


def test_a_runaway_download_is_stopped_before_it_fills_the_disk(tmp_path):
    def opener(url):
        return "application/json", (b"x" * 1000 for _ in range(1000))

    result = fetch_one(ROW, str(tmp_path), opener=opener, max_bytes=5000)

    assert result.status == "too_big"
    assert not result.ok
    assert list(tmp_path.iterdir()) == []


def test_an_empty_response_is_an_error_not_an_empty_file(tmp_path):
    result = fetch_one(ROW, str(tmp_path), opener=_opener([]))

    assert result.status == "error"
    assert "empty" in result.note
    assert list(tmp_path.iterdir()) == []


def test_an_unreachable_host_is_recorded_and_the_run_continues(tmp_path):
    def opener(url):
        raise TimeoutError("no route to host")

    result = fetch_one(ROW, str(tmp_path), opener=opener)
    assert result.status == "error"
    assert "TimeoutError" in result.note


def test_a_row_with_no_url_is_refused_before_any_request(tmp_path):
    called = []

    def opener(url):
        called.append(url)
        raise AssertionError("should not be reached")

    result = fetch_one({"ccn": "170027", "name": "X"}, str(tmp_path), opener=opener)
    assert result.status == "error"
    assert called == []


# --- resuming a run -------------------------------------------------------


def test_a_file_already_on_disk_is_not_downloaded_again(tmp_path):
    fetch_one(ROW, str(tmp_path), opener=_opener([b"abcdef"]))

    def opener(url):
        raise AssertionError("should not re-download")

    result = fetch_one(ROW, str(tmp_path), opener=opener)
    assert result.status == "skipped"
    assert result.ok
    assert result.bytes_written == 6


def test_overwrite_re_downloads_deliberately(tmp_path):
    fetch_one(ROW, str(tmp_path), opener=_opener([b"old"]))
    result = fetch_one(ROW, str(tmp_path), opener=_opener([b"newer"]), overwrite=True)

    assert result.status == "ok"
    assert open(result.path, "rb").read() == b"newer"


def test_the_destination_is_created_if_it_does_not_exist(tmp_path):
    dest = tmp_path / "round8"
    result = fetch_one(ROW, str(dest), opener=_opener([b"abc"]))
    assert result.status == "ok"
    assert dest.is_dir()


def test_no_part_file_survives_a_successful_download(tmp_path):
    fetch_one(ROW, str(tmp_path), opener=_opener([b"abc"]))
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")] == []


def test_two_hospitals_do_not_collide_on_one_filename(tmp_path):
    other = dict(ROW, ccn="170045", name="VIA CHRISTI ST FRANCIS")
    a = fetch_one(ROW, str(tmp_path), opener=_opener([b"a"]))
    b = fetch_one(other, str(tmp_path), opener=_opener([b"b"]))

    assert a.path != b.path
    assert len(list(tmp_path.iterdir())) == 2


def test_a_fetched_result_knows_whether_it_needs_retrying():
    assert Fetched("1", "u", status="ok").ok
    assert Fetched("1", "u", status="skipped").ok
    assert not Fetched("1", "u", status="error").ok
    assert not Fetched("1", "u", status="too_big").ok


def test_a_resume_does_not_open_a_connection_at_all(tmp_path):
    """Checking the disk first is the difference between a resume over 2,300
    hospitals and 2,300 requests to hospital web servers."""

    fetch_one(ROW, str(tmp_path), opener=_opener([b"abcdef"]))

    opened = []

    def opener(url):
        opened.append(url)
        return "application/json", iter([b"x"])

    fetch_one(ROW, str(tmp_path), opener=opener)
    assert opened == []


def test_a_file_saved_under_a_different_extension_still_counts_as_done(tmp_path):
    """The same URL yields .json or .csv depending only on the Content-Type the
    server happened to send, so a resume cannot match on extension."""

    row = dict(ROW, mrf_url="https://prmc.org/download")
    first = fetch_one(row, str(tmp_path), opener=_opener([b"abc"], "application/json"))
    assert first.path.endswith(".json")

    second = fetch_one(row, str(tmp_path), opener=_opener([b"abc"], "text/csv"))
    assert second.status == "skipped"
    assert len(list(tmp_path.iterdir())) == 1


def test_a_leftover_part_file_does_not_count_as_a_finished_download(tmp_path):
    (tmp_path / "ccn-170027_pratt-regional-medical-center_standardcharges.json.part").write_bytes(b"x")

    result = fetch_one(ROW, str(tmp_path), opener=_opener([b"abcdef"]))
    assert result.status == "ok"
