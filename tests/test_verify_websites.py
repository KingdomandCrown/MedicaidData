import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "verify_hospital_websites.py")
_spec = importlib.util.spec_from_file_location("verify_hospital_websites", SCRIPT)
ver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ver)

HOSPITAL = {
    "ccn": "170012",
    "hospital_name": "Sunflower General Hospital",
    "city": "Wichita",
    "state": "KS",
    "zip": "67214",
    "phone": "3165550100",
}


def test_registrable_domain():
    assert ver.registrable_domain("https://www.wesleymc.com/about") == "wesleymc.com"
    assert ver.registrable_domain("http://hospital.ascension.org/x") == "ascension.org"
    assert ver.registrable_domain("not a url") == ""


def test_aggregators_flagged():
    assert ver.is_aggregator("https://www.facebook.com/sunflowerhospital")
    assert ver.is_aggregator("https://en.wikipedia.org/wiki/Sunflower")
    assert not ver.is_aggregator("https://sunflowerhospital.org")


def test_rank_candidates_prefers_cross_source_agreement():
    ranked = ver.rank_candidates([
        ("https://www.sunflower.org", "wikidata"),
        ("https://sunflower.org/home", "osm"),
        ("https://other-place.com", "places"),
    ])
    assert ranked[0]["domain"] == "sunflower.org"
    assert ranked[0]["sources"] == {"wikidata", "osm"}
    assert ranked[1]["domain"] == "other-place.com"


def test_evaluate_page_matches_known_facts():
    html = """
    <html><head><title>Sunflower General Hospital | Wichita, KS</title></head>
    <body><script>var x = 1;</script>
    <p>Call us at (316) 555-0100. 1234 Main St, Wichita, KS 67214.</p>
    </body></html>
    """
    checks = ver.evaluate_page(html, HOSPITAL)
    assert checks == {
        "name_match": True, "city_match": True, "zip_match": True, "phone_match": True,
    }

    unrelated = "<html><body>Joe's Diner, best pancakes in Topeka</body></html>"
    checks = ver.evaluate_page(unrelated, HOSPITAL)
    assert not any(checks.values())


def test_decide_verdicts():
    strong = {"name_match": True, "city_match": True, "zip_match": False, "phone_match": False}
    weak = {"name_match": False, "city_match": False, "zip_match": False, "phone_match": False}
    phone_only = {"name_match": False, "city_match": False, "zip_match": False, "phone_match": True}

    assert ver.decide(True, False, 1, strong) == "VERIFIED"
    assert ver.decide(True, False, 1, phone_only) == "VERIFIED"
    assert ver.decide(True, False, 2, weak) == "VERIFIED"  # cross-source agreement
    assert ver.decide(True, False, 1, weak) == "REVIEW"
    assert ver.decide(True, True, 2, strong) == "REVIEW"  # aggregator never verifies
    assert ver.decide(False, False, 2, weak) == "DEAD"


def test_verify_one_offline(monkeypatch):
    def fake_fetch(url, session):
        if "sunflower.org" in url:
            return 200, url, "<title>Sunflower General Hospital Wichita</title>", ""
        return 0, url, "", "connection refused"

    monkeypatch.setattr(ver, "fetch", fake_fetch)

    row = ver.verify_one(
        HOSPITAL,
        [("https://deadsite.com", "places"), ("https://sunflower.org", "osm")],
        session=None,
    )
    assert row["verdict"] == "VERIFIED"
    assert row["domain"] == "sunflower.org"

    row = ver.verify_one(HOSPITAL, [], session=None)
    assert row["verdict"] == "NO_CANDIDATE"

    row = ver.verify_one(HOSPITAL, [("https://deadsite.com", "places")], session=None)
    assert row["verdict"] == "DEAD"
    assert row["error"] == "connection refused"
