import importlib.util
import os

# Standalone script, loaded by file path like the other resolver tests.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "resolve_hospital_websites_free.py")
_spec = importlib.util.spec_from_file_location("resolve_hospital_websites_free", SCRIPT)
free = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(free)


def test_normalize_website_adds_scheme():
    assert free.normalize_website("example.org") == "https://example.org"
    assert free.normalize_website("http://example.org") == "http://example.org"
    assert free.normalize_website("HTTPS://example.org") == "HTTPS://example.org"
    assert free.normalize_website("") == ""


def test_parse_wikidata_prefers_state_resolved_duplicates():
    bindings = [
        {  # stateless duplicate first
            "hospitalLabel": {"value": "Wesley Medical Center"},
            "website": {"value": "https://wesleymc.com"},
        },
        {
            "hospitalLabel": {"value": "Wesley Medical Center"},
            "website": {"value": "https://wesleymc.com"},
            "stateLabel": {"value": "Kansas"},
        },
        {  # unlabeled item comes back as its Q-id -> dropped
            "hospitalLabel": {"value": "Q99999999"},
            "website": {"value": "https://nolabel.example"},
        },
    ]
    cands = free.parse_wikidata_bindings(bindings)
    assert len(cands) == 1
    assert cands[0].state == "KS"
    assert cands[0].source == "wikidata"


def test_parse_overpass_elements():
    elements = [
        {"tags": {"name": "Prairie Hospital", "website": "prairiehospital.org", "addr:city": "Hays"}},
        {"tags": {"name": "No Website Clinic"}},
        {"tags": {"name": "Contact Tagged", "contact:website": "https://ct.example.org"}},
    ]
    cands = free.parse_overpass_elements(elements, "KS")
    assert [c.name for c in cands] == ["Prairie Hospital", "Contact Tagged"]
    assert cands[0].website == "https://prairiehospital.org"
    assert cands[0].city == "Hays"
    assert all(c.state == "KS" and c.source == "osm" for c in cands)


def test_score_high_needs_near_exact_or_city_agreement():
    exact = free.Candidate("Wesley Medical Center", "https://w.example", "KS", None, "osm")
    assert free.score("Wesley Medical Center", "Wichita", exact)[1] == "HIGH"

    close_with_city = free.Candidate(
        "Wesley Medical Center - Main Campus", "https://w.example", "KS", "Wichita", "osm"
    )
    sim, conf = free.score("Wesley Medical Center", "Wichita", close_with_city)
    assert conf == "HIGH" and sim < 0.90

    close_no_city = free.Candidate(
        "Wesley Medical Center - Main Campus", "https://w.example", "KS", None, "osm"
    )
    assert free.score("Wesley Medical Center", "Wichita", close_no_city)[1] == "MEDIUM"

    unrelated = free.Candidate("Topeka Eye Clinic", "https://t.example", "KS", None, "osm")
    assert free.score("Wesley Medical Center", "Wichita", unrelated)[1] == "NONE"


def test_stateless_candidates_never_high():
    stateless = free.Candidate("Wesley Medical Center", "https://w.example", None, None, "wikidata")
    sim, conf = free.score("Wesley Medical Center", "Wichita", stateless)
    assert sim == 1.0
    assert conf == "MEDIUM"


def test_best_match_prefers_confidence_then_similarity():
    row = {"ccn": "170012", "hospital_name": "Wesley Medical Center", "city": "Wichita", "state": "KS"}
    by_state = {
        "KS": [
            free.Candidate("Wesley Medical Center - Main Campus", "https://a.example", "KS", None, "osm"),
            free.Candidate("Wesley Medical Center", "https://b.example", "KS", None, "osm"),
        ]
    }
    cand, sim, conf = free.best_match(row, by_state, [])
    assert cand.website == "https://b.example"
    assert conf == "HIGH"

    # A hospital in a state with no candidates gets no match.
    md_row = {"ccn": "210001", "hospital_name": "Chesapeake Regional", "city": "Baltimore", "state": "MD"}
    assert free.best_match(md_row, by_state, []) is None
