"""Finding the file a hospital is required to publish.

The tests that matter are about a health system's shared ``cms-hpt.txt``.
Picking the wrong block there attributes one hospital's negotiated rates to
another, and nothing downstream can detect it: the file that arrives is
perfectly valid, just not this hospital's.
"""

import pytest

from hospitals.mrf_discovery import (
    MATCH_THRESHOLD,
    Discovery,
    discover_one,
    hpt_urls,
    match_location,
    name_similarity,
    parse_cms_hpt,
    significant_tokens,
    site_root,
    to_row,
)

# --- reading the file -----------------------------------------------------


SINGLE = """
Location-Name: Pratt Regional Medical Center
Source-Page-URL: https://prmc.org/pricing
MRF-URL: https://prmc.org/480598437_pratt-regional_standardcharges.json
"""


def test_a_single_facility_file_yields_one_record():
    records = parse_cms_hpt(SINGLE)
    assert len(records) == 1
    assert records[0].location_name == "Pratt Regional Medical Center"
    assert records[0].mrf_url.endswith("standardcharges.json")


def test_underscores_and_equals_are_read_the_same_as_hyphens_and_colons():
    """CMS's examples use one form; real files use all four combinations."""

    records = parse_cms_hpt(
        "Location_Name = St Francis\nMRF_URL = https://x.org/a.json\n"
    )
    assert records[0].location_name == "St Francis"
    assert records[0].mrf_url == "https://x.org/a.json"


def test_windows_line_endings_are_read():
    records = parse_cms_hpt("Location-Name: A\r\nMRF-URL: https://x.org/a.json\r\n")
    assert len(records) == 1


def test_a_new_location_name_starts_a_new_record():
    """The format has no block delimiter; a repeated key is the only signal."""

    body = (
        "Location-Name: Alpha\nMRF-URL: https://x.org/alpha.json\n"
        "Location-Name: Beta\nMRF-URL: https://x.org/beta.json\n"
        "Location-Name: Gamma\nMRF-URL: https://x.org/gamma.json\n"
    )
    records = parse_cms_hpt(body)
    assert [r.location_name for r in records] == ["Alpha", "Beta", "Gamma"]


def test_a_block_with_no_url_is_not_a_record():
    body = "Location-Name: Alpha\nContact: someone\nLocation-Name: Beta\nMRF-URL: https://x.org/b.json\n"
    assert [r.location_name for r in parse_cms_hpt(body)] == ["Beta"]


def test_a_page_that_is_not_an_hpt_file_yields_nothing():
    assert parse_cms_hpt("<html><body>404 Not Found</body></html>") == []


def test_an_empty_body_yields_nothing():
    assert parse_cms_hpt("") == []


# --- matching a facility --------------------------------------------------


def test_generic_words_are_not_what_makes_a_name_distinctive():
    """'Memorial Hospital' should not match on the strength of 'memorial'."""

    assert "memorial" not in significant_tokens("Pratt Memorial Hospital")
    assert "pratt" in significant_tokens("Pratt Memorial Hospital")


def test_a_name_of_nothing_but_generic_words_still_has_tokens():
    assert significant_tokens("Community Medical Center")


def test_similarity_is_zero_against_nothing():
    assert name_similarity("Pratt", None) == 0.0
    assert name_similarity(None, "Pratt") == 0.0


SYSTEM = [
    ("HCA HealthONE Sky Ridge", "https://x.org/sky-ridge.json"),
    ("HCA HealthONE Presbyterian St Luke's", "https://x.org/pslmc.json"),
    ("HCA HealthONE Rose", "https://x.org/rose.json"),
]


def _records(pairs):
    body = "".join(
        f"Location-Name: {name}\nMRF-URL: {url}\n" for name, url in pairs
    )
    return parse_cms_hpt(body)


def test_one_facility_needs_no_name_match():
    """A hospital publishing its own MRF at its own domain is the normal case.

    Requiring a name match here would reject correct files over spelling.
    """

    match = match_location("Totally Different Name", _records(SINGLE_PAIR))
    assert match.is_confident
    assert match.score == 1.0


SINGLE_PAIR = [("Pratt Regional Medical Center", "https://prmc.org/a.json")]


def test_the_right_facility_is_picked_out_of_a_system_file():
    match = match_location("HCA HEALTHONE SKY RIDGE MEDICAL CENTER", _records(SYSTEM))
    assert match.is_confident
    assert match.record.mrf_url.endswith("sky-ridge.json")


def test_a_hospital_not_in_the_system_file_matches_nothing():
    match = match_location("PRATT REGIONAL MEDICAL CENTER", _records(SYSTEM))
    assert not match.is_confident
    assert match.score < MATCH_THRESHOLD


def test_a_tie_is_not_a_match_however_high_it_scores():
    """On a system domain the runner-up is a different real hospital."""

    pairs = [
        ("Mercy Hospital Springfield", "https://x.org/a.json"),
        ("Mercy Hospital Springfield", "https://x.org/b.json"),
    ]
    match = match_location("Mercy Hospital Springfield", _records(pairs))
    assert not match.is_confident


# --- the website ----------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://www.prmc.org/pricing", "prmc.org"),
        ("prmc.org", "prmc.org"),
        ("http://PRMC.ORG", "prmc.org"),
        ("https://sub.prmc.org/", "sub.prmc.org"),
        ("", ""),
        (None, ""),
    ],
)
def test_a_website_is_reduced_to_the_host_the_file_lives_at(given, expected):
    assert site_root(given) == expected


def test_nothing_is_looked_up_for_a_hospital_with_no_website():
    assert hpt_urls("") == []


def test_both_bare_and_www_are_tried():
    urls = hpt_urls("prmc.org")
    assert "https://prmc.org/cms-hpt.txt" in urls
    assert "https://www.prmc.org/cms-hpt.txt" in urls


# --- one hospital, end to end ---------------------------------------------


def _fetcher(pages):
    def fetch(url):
        return pages.get(url)

    return fetch


PRATT = {"ccn": "170027", "name": "PRATT REGIONAL MEDICAL CENTER", "state": "KS",
         "website": "https://prmc.org"}


def test_a_hospital_publishing_its_own_file_is_found():
    fetch = _fetcher({"https://prmc.org/cms-hpt.txt": SINGLE})
    (row,) = discover_one(PRATT, fetch)

    assert row.status == "found"
    assert row.mrf_url.endswith("standardcharges.json")
    assert row.ccn == "170027"


def test_a_relative_url_is_resolved_against_the_page_it_came_from():
    fetch = _fetcher({
        "https://prmc.org/cms-hpt.txt":
            "Location-Name: Pratt\nMRF-URL: /files/charges.json\n",
    })
    (row,) = discover_one(PRATT, fetch)
    assert row.mrf_url == "https://prmc.org/files/charges.json"


def test_the_www_host_is_tried_when_the_bare_one_has_nothing():
    fetch = _fetcher({"https://www.prmc.org/cms-hpt.txt": SINGLE})
    (row,) = discover_one(PRATT, fetch)
    assert row.status == "found"


def test_a_404_page_returned_with_status_200_is_not_an_hpt_file():
    """The most common thing a hospital website serves at that path."""

    fetch = _fetcher({"https://prmc.org/cms-hpt.txt": "<html>Page not found</html>"})
    (row,) = discover_one(PRATT, fetch)
    assert row.status == "no_hpt"


def test_a_hospital_with_no_website_says_so_rather_than_failing():
    (row,) = discover_one({"ccn": "170027", "name": "X", "website": None}, _fetcher({}))
    assert row.status == "no_website"
    assert "no website" in row.note


def test_an_unreachable_site_is_recorded_as_a_gap_not_a_crash():
    (row,) = discover_one(PRATT, lambda url: None)
    assert row.status == "no_hpt"


# --- the case that must not guess -----------------------------------------


def test_an_ambiguous_system_file_records_every_candidate():
    """Picking wrong here is invisible afterwards: the file is valid, just
    somebody else's. So the crawler declines and a person chooses."""

    body = "".join(f"Location-Name: {n}\nMRF-URL: {u}\n" for n, u in SYSTEM)
    fetch = _fetcher({"https://x.org/cms-hpt.txt": body})
    rows = discover_one(
        {"ccn": "060112", "name": "SOME UNRELATED HOSPITAL", "state": "CO",
         "website": "https://x.org"},
        fetch,
    )

    assert len(rows) == 3
    assert {r.status for r in rows} == {"ambiguous"}
    assert all(r.mrf_url for r in rows)
    assert "3 facilities share x.org" in rows[0].note


def test_ambiguous_candidates_are_ordered_by_how_close_they_came():
    body = "".join(f"Location-Name: {n}\nMRF-URL: {u}\n" for n, u in SYSTEM)
    fetch = _fetcher({"https://x.org/cms-hpt.txt": body})
    rows = discover_one(
        {"ccn": "060112", "name": "HealthONE Rose Ridge", "state": "CO",
         "website": "https://x.org"},
        fetch,
    )
    assert rows[0].score >= rows[-1].score


def test_no_row_is_actionable_while_it_is_ambiguous():
    body = "".join(f"Location-Name: {n}\nMRF-URL: {u}\n" for n, u in SYSTEM)
    fetch = _fetcher({"https://x.org/cms-hpt.txt": body})
    rows = discover_one(
        {"ccn": "060112", "name": "UNRELATED", "website": "https://x.org"}, fetch
    )
    assert not any(r.is_actionable for r in rows)


# --- a system's file is fetched once --------------------------------------


def test_hospitals_sharing_a_domain_fetch_it_once():
    """Twenty hospitals in one system is one request, not twenty."""

    calls = []

    def fetch(url):
        calls.append(url)
        return SINGLE if url == "https://x.org/cms-hpt.txt" else None

    cache = {}
    for ccn in ("060112", "060113", "060114"):
        discover_one({"ccn": ccn, "name": "Pratt Regional Medical Center",
                      "website": "https://x.org"}, fetch, cache=cache)

    assert calls == ["https://x.org/cms-hpt.txt"]


def test_a_site_with_nothing_is_also_only_tried_once():
    calls = []

    def fetch(url):
        calls.append(url)
        return None

    cache = {}
    for ccn in ("1", "2"):
        discover_one({"ccn": ccn, "name": "X", "website": "https://y.org"}, fetch,
                     cache=cache)

    assert len(calls) == 3  # the three spellings, for the first hospital only


# --- the manifest ---------------------------------------------------------


def test_a_row_carries_no_none_values_into_the_csv():
    row = to_row(Discovery(ccn="170027", name="PRATT"))
    assert all(isinstance(v, str) for v in row.values())


# --- telling one Kaiser from another --------------------------------------


KAISER = [
    ("Kaiser Foundation Hospital - Fresno", "https://kp.org/fresno.json"),
    ("Kaiser Foundation Hospital - Anaheim", "https://kp.org/anaheim.json"),
    ("Kaiser Foundation Hospital - Roseville", "https://kp.org/roseville.json"),
]


def test_the_city_tells_one_facility_of_a_system_from_another():
    """California produced 98 ambiguous rows against 15 for four midwest states.

    A large system's facilities differ by place, not by name. Comparing names
    alone scores every "Kaiser Foundation Hospital" identically and correctly
    refuses to choose; the city is the fact that makes an answer possible.
    """

    records = _records(KAISER)

    assert not match_location("KAISER FOUNDATION HOSPITAL", records).is_confident

    match = match_location("KAISER FOUNDATION HOSPITAL", records, city="Fresno")
    assert match.is_confident
    assert match.record.mrf_url.endswith("fresno.json")


def test_a_city_nobody_lists_still_refuses_to_guess():
    records = _records(KAISER)
    assert not match_location("KAISER FOUNDATION HOSPITAL", records,
                              city="Bakersfield").is_confident


def test_two_facilities_in_one_city_are_still_ambiguous():
    """The city separates Kaiser Fresno from Kaiser Anaheim. It cannot separate
    two hospitals a system runs in the same place."""

    records = _records([
        ("Sutter Health Sacramento Midtown", "https://x.org/a.json"),
        ("Sutter Health Sacramento Downtown", "https://x.org/b.json"),
    ])
    assert not match_location("SUTTER HEALTH", records, city="Sacramento").is_confident


def test_the_city_never_overrides_a_clear_name_match():
    records = _records([
        ("Mercy Hospital Joplin", "https://x.org/joplin.json"),
        ("Mercy Hospital Springfield", "https://x.org/springfield.json"),
    ])

    match = match_location("MERCY HOSPITAL JOPLIN", records, city="Springfield")
    assert match.record.mrf_url.endswith("joplin.json")


def test_a_hospital_with_no_city_on_record_behaves_as_before():
    records = _records(SYSTEM)
    assert match_location("HCA HEALTHONE SKY RIDGE", records, city=None).is_confident


def test_the_city_reaches_the_matcher_from_the_hospital_record():
    body = "".join(f"Location-Name: {n}\nMRF-URL: {u}\n" for n, u in KAISER)
    fetch = _fetcher({"https://kp.org/cms-hpt.txt": body})

    (row,) = discover_one(
        {"ccn": "050515", "name": "KAISER FOUNDATION HOSPITAL", "state": "CA",
         "city": "Fresno", "website": "https://kp.org"},
        fetch,
    )

    assert row.status == "found"
    assert row.mrf_url.endswith("fresno.json")
