import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "merge_website_captures.py")
_spec = importlib.util.spec_from_file_location("merge_website_captures", SCRIPT)
merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge)


def test_priority_order_first_capture_wins(tmp_path):
    a = tmp_path / "manual.csv"
    a.write_text("ccn,website_uri,source\n170012,https://manual.example,manual\n")
    b = tmp_path / "ahd.csv"
    b.write_text(
        "ccn,website_uri,system_website,operating_status,source\n"
        "170012,https://ahd.example,,Operating,ahd\n"
        "210001,https://chesapeake.example,https://sys.example,Operating,ahd\n"
    )
    best = merge.load_captures([str(a), str(b)])
    assert best["170012"]["website_uri"] == "https://manual.example"
    assert best["170012"]["website_source"] == "manual"
    assert best["210001"]["system_website"] == "https://sys.example"


def test_source_falls_back_to_filename(tmp_path):
    c = tmp_path / "places_run.csv"
    c.write_text("ccn,website_uri\n170045,https://x.example\n")
    best = merge.load_captures([str(c)])
    assert best["170045"]["website_source"] == "places_run"
