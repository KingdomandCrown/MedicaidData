"""Regression for MRFs that skip the CMS two-row metadata preamble.

Five files in a 399-file batch parsed cleanly and produced zero rows: they put
the data header on line 1, so the reader consumed the first data rows as the
preamble and then mapped every column to nothing. The header block is now
located by name instead of assumed to be line 3.
"""

import os

import pytest

from hospitals import price_transparency as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HEADER_ONLY = os.path.join(FIX, "mrf_headeronly_sample.csv")
SPACER = os.path.join(FIX, "mrf_spacer_sample.csv")
TALL = os.path.join(FIX, "mrf_tall_sample.csv")
WIDE = os.path.join(FIX, "mrf_wide_sample.csv")


# --- header detection -----------------------------------------------------


def test_data_header_is_recognized():
    assert pt._looks_like_data_header(["description", "code|1", "payer_name"])
    assert pt._looks_like_data_header(["Description", "Setting"])
    assert pt._looks_like_data_header(["x", "standard_charge|AETNA|PPO|negotiated_dollar"])


def test_metadata_header_is_not_mistaken_for_a_data_header():
    assert not pt._looks_like_data_header(
        ["hospital_name", "last_updated_on", "version", "type_2_npi", "license_number|MD"]
    )


# --- header-only files ----------------------------------------------------


def test_header_on_line_one_yields_rows():
    """The zero-row failure: no preamble, data header first."""

    meta, rows = pt.read_any(HEADER_ONLY)
    rows = list(rows)
    assert meta.layout == "tall"
    assert len(rows) == 3
    assert rows[0].description == "CT scan head w/o contrast"
    assert str(rows[0].negotiated_dollar) == "640.50"
    assert rows[0].payer_name == "Aetna"


def test_header_on_line_one_still_keys_on_the_filename_ein(tmp_path):
    named = tmp_path / "383870608_christ-hospital_standardcharges.csv"
    named.write_bytes(open(HEADER_ONLY, "rb").read())

    meta, rows = pt.read_any(str(named))
    assert meta.ein == "383870608"
    assert meta.hospital_name is None  # nothing in the file to read it from
    assert len(list(rows)) == 3


# --- blank spacer rows ----------------------------------------------------


def test_blank_row_between_preamble_and_header_is_skipped():
    meta, rows = pt.read_any(SPACER)
    rows = list(rows)
    assert meta.hospital_name == "Prairie Community Hospital"
    assert meta.primary_npi == "1114052274"
    assert len(rows) == 2
    assert rows[0].description == "Chest x-ray 2 views"


# --- conforming files are unchanged ---------------------------------------


def test_standard_preamble_still_parses():
    meta, rows = pt.read_any(TALL)
    rows = list(rows)
    assert meta.hospital_name == "Sunflower General Hospital"
    assert meta.primary_npi == "1578597993"
    assert meta.license_state == "MD"
    assert len(rows) == 3


def test_wide_preamble_still_parses():
    meta, rows = pt.read_any(WIDE)
    assert meta.layout == "wide"
    assert meta.hospital_name is not None
    assert list(rows)


# --- unreadable files fail loudly -----------------------------------------


def test_a_file_without_a_data_header_raises(tmp_path):
    junk = tmp_path / "nope.csv"
    junk.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError, match="data header"):
        pt.read_any(str(junk))


def test_an_empty_file_raises(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    with pytest.raises(ValueError, match="ended before the data header"):
        pt.read_any(str(empty))
