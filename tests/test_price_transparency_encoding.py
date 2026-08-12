"""Regressions for real-world encoding quirks seen in a 399-file batch.

Two classes of failure accounted for most of the batch's rejected files:

* CSVs exported from Excel on Windows arrive in cp1252, so a curly apostrophe
  is byte 0x92 and a strict UTF-8 read raises ``invalid start byte``.
* JSON files saved with a UTF-8 byte order mark put three bytes before the
  opening brace, which a byte-level JSON parser rejects.

Both fixtures reproduce the exact bytes that failed.
"""

import os

from hospitals import price_transparency as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
CP1252_CSV = os.path.join(FIX, "mrf_cp1252_sample.csv")
CP1252_ZIP = os.path.join(FIX, "mrf_cp1252_sample.zip")
BOM_JSON = os.path.join(FIX, "mrf_bom_sample.json")
UTF8_CSV = os.path.join(FIX, "mrf_tall_sample.csv")


# --- encoding detection ---------------------------------------------------


def test_detect_encoding_prefers_utf8():
    assert pt.detect_encoding("plain ascii".encode("utf-8")) == "utf-8-sig"
    assert pt.detect_encoding("café — dash".encode("utf-8")) == "utf-8-sig"


def test_detect_encoding_falls_back_to_cp1252():
    # 0x92 is a cp1252 curly apostrophe and is not valid UTF-8.
    assert pt.detect_encoding(b"Children\x92s Hospital") == "cp1252"
    assert pt.detect_encoding(b"quoted \x94text\x94") == "cp1252"


def test_detect_encoding_honours_a_bom():
    assert pt.detect_encoding(b"\xef\xbb\xbfhospital_name") == "utf-8-sig"


def test_a_split_multibyte_char_at_the_tail_is_not_cp1252():
    """A chunk boundary mid-character must not be read as evidence of cp1252."""

    sample = ("x" * 50).encode("utf-8") + "é".encode("utf-8")[:1]
    assert pt.detect_encoding(sample) == "utf-8-sig"


# --- cp1252 CSV -----------------------------------------------------------


def test_cp1252_csv_parses_and_keeps_the_character():
    """The file that raised 'invalid start byte' now reads, without mojibake."""

    raw = open(CP1252_CSV, "rb").read()
    assert b"\x92" in raw, "fixture must contain the byte that broke the real files"

    meta, rows = pt.read_any(CP1252_CSV)
    rows = list(rows)
    assert meta.hospital_name == "Sunflower General Hospital"
    assert len(rows) == 3
    # The curly apostrophe decodes to the real character, not a replacement.
    assert rows[0].description == "Children’s CT scan head"
    assert "�" not in rows[0].description


def test_cp1252_inside_a_zip_parses():
    meta, rows = pt.read_any(CP1252_ZIP)
    rows = list(rows)
    assert meta.layout == "tall"
    assert rows[0].description == "Children’s CT scan head"


def test_utf8_files_are_unaffected():
    """Detection must not mangle files that really are UTF-8."""

    meta, rows = pt.read_any(UTF8_CSV)
    rows = list(rows)
    assert meta.hospital_name == "Sunflower General Hospital"
    assert len(rows) == 3
    assert rows[0].description == "CT scan head w/o contrast"


# --- JSON BOM -------------------------------------------------------------


def test_json_with_a_bom_parses():
    """The Berger / Bayhealth failure: three BOM bytes before the opening brace."""

    assert open(BOM_JSON, "rb").read(3) == b"\xef\xbb\xbf"

    meta, rows = pt.read_any(BOM_JSON)
    rows = list(rows)
    assert meta.layout == "json"
    assert meta.hospital_name == "Cascade Regional Medical Center"
    assert len(rows) == 3


def test_bom_stripper_passes_through_a_stream_without_a_bom():
    import io

    s = pt._BomStrippedBinary(io.BytesIO(b'{"a": 1}'))
    assert s.read() == b'{"a": 1}'


def test_bom_stripper_handles_small_reads():
    import io

    s = pt._BomStrippedBinary(io.BytesIO(b"\xef\xbb\xbf" + b"abcdef"))
    assert s.read(2) == b"ab"
    assert s.read() == b"cdef"
