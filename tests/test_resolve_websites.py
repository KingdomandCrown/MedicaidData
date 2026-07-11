import importlib.util
import os

# The resolver is a standalone script (not part of the package), so load it
# by file path. Only the pure helpers are tested here; the Places API call
# itself is network-bound and exercised with --limit on a real run.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "resolve_hospital_websites.py")
_spec = importlib.util.spec_from_file_location("resolve_hospital_websites", SCRIPT)
rhw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rhw)


def test_normalize_strips_punctuation_and_case():
    assert rhw.normalize("St. Mary's  Medical Center!") == "st mary s medical center"
    assert rhw.normalize(None) == ""
    assert rhw.normalize("") == ""


def test_match_confidence_high_on_name_and_city():
    conf = rhw.match_confidence(
        "Wesley Medical Center",
        "Wichita",
        "Wesley Medical Center",
        "550 N Hillside St, Wichita, KS 67214, USA",
    )
    assert conf == "HIGH"


def test_match_confidence_medium_on_city_only():
    conf = rhw.match_confidence(
        "Wesley Medical Center",
        "Wichita",
        "Completely Different Clinic",
        "123 Main St, Wichita, KS 67202, USA",
    )
    assert conf == "MEDIUM"


def test_match_confidence_low_on_no_overlap():
    conf = rhw.match_confidence(
        "Wesley Medical Center",
        "Wichita",
        "Joe's Diner",
        "9 Elm St, Topeka, KS 66603, USA",
    )
    assert conf == "LOW"


def test_build_query_includes_all_parts_and_hospital_hint():
    q = rhw.build_query("Wesley Medical Center", "550 N Hillside St", "Wichita", "KS", "67214")
    assert q == "Wesley Medical Center 550 N Hillside St Wichita KS 67214 hospital"


def test_build_query_skips_blank_parts():
    q = rhw.build_query("Wesley Medical Center", "", "Wichita", "KS", "")
    assert q == "Wesley Medical Center Wichita KS hospital"


def test_load_checkpoint_reads_done_ccns(tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("ccn,website_uri\n170001,https://example.org\n170123,\n")
    assert rhw.load_checkpoint(str(path)) == {"170001", "170123"}


def test_load_checkpoint_missing_file_is_empty(tmp_path):
    assert rhw.load_checkpoint(str(tmp_path / "nope.csv")) == set()
