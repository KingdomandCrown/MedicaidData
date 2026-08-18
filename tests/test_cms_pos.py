from hospitals import cms_pos
from hospitals.cms_pos import PosDistribution


def test_iter_csv_records_reads_fixture(fixture_csv):
    rows = list(cms_pos.iter_csv_records(fixture_csv))
    assert len(rows) == 7
    assert rows[0]["PRVDR_NUM"] == "170012"
    assert rows[0]["FAC_NAME"] == "SUNFLOWER GENERAL HOSPITAL"


def test_fetch_records_from_input_file_with_limit(fixture_csv):
    rows = list(cms_pos.fetch_records(input_file=fixture_csv, limit=3))
    assert len(rows) == 3


def test_discover_latest_distribution_picks_newest(monkeypatch):
    items = [
        {
            "identifier": "dataset-1",
            "title": cms_pos.POS_DATASET_TITLE,
            "modified": "2024-01-01",
            "distribution": [
                {"data": {"identifier": "old-uuid", "modified": "2023-12-01"}},
                {"data": {"identifier": "new-uuid", "modified": "2024-09-30"}},
            ],
        }
    ]

    def fake_get_json(url, params, session):
        return items

    monkeypatch.setattr(cms_pos, "_get_json", fake_get_json)
    dist = cms_pos.discover_latest_distribution()
    assert isinstance(dist, PosDistribution)
    assert dist.distribution_id == "new-uuid"
    assert dist.modified == "2024-09-30"
    assert dist.data_api_url.endswith("/new-uuid/data")


def test_iter_data_api_records_paginates(monkeypatch):
    dist = PosDistribution("d", "uuid", "t", "2024-01-01", None)
    pages = {
        0: [{"PRVDR_NUM": f"1700{i:02d}"} for i in range(cms_pos.DEFAULT_PAGE_SIZE)],
        cms_pos.DEFAULT_PAGE_SIZE: [{"PRVDR_NUM": "170099"}],
    }

    def fake_get_json(url, params, session):
        return pages.get(params["offset"], [])

    monkeypatch.setattr(cms_pos, "_get_json", fake_get_json)
    rows = list(cms_pos.iter_data_api_records(dist, state_usps="KS"))
    assert len(rows) == cms_pos.DEFAULT_PAGE_SIZE + 1


def test_cms_unavailable_raised_on_request_error(monkeypatch):
    import requests

    class Boom(requests.Session):
        def get(self, *a, **k):
            raise requests.ConnectionError("blocked by egress policy")

    with __import__("pytest").raises(cms_pos.CmsUnavailableError):
        cms_pos.discover_latest_distribution(session=Boom())


# --- a 200 that is not JSON ------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, text, content_type):
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": content_type}

    def json(self):
        import json

        return json.loads(self.text)


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        return self._response


def test_html_served_with_a_200_names_what_came_back():
    """`hospitals ingest --state ALL` died on "Expecting value: line 1 column 1".

    True, and useless: it says the first character was not a brace. The body
    was an HTML block page — something answering in CMS's place — and that is
    the fact that tells you where to look.
    """

    import pytest

    from hospitals import cms_pos

    page = "<!DOCTYPE html><html><head><title>Access Denied</title></head>"
    session = _FakeSession(_FakeResponse(200, page, "text/html; charset=utf-8"))

    with pytest.raises(cms_pos.CmsUnavailableError) as excinfo:
        cms_pos._get_json("https://data.cms.gov/x", None, session)

    message = str(excinfo.value)
    assert "not JSON" in message
    assert "text/html" in message
    assert "Access Denied" in message


def test_a_json_body_is_still_returned_untouched():
    from hospitals import cms_pos

    session = _FakeSession(_FakeResponse(200, '[{"title": "POS"}]', "application/json"))
    assert cms_pos._get_json("https://data.cms.gov/x", None, session) == [{"title": "POS"}]


def test_an_http_error_still_reports_its_status():
    import pytest

    from hospitals import cms_pos

    session = _FakeSession(_FakeResponse(503, "upstream down", "text/plain"))
    with pytest.raises(cms_pos.CmsUnavailableError, match="HTTP 503"):
        cms_pos._get_json("https://data.cms.gov/x", None, session)


def test_the_session_asks_for_json():
    """data.cms.gov served its own SPA to a client that did not say what it wanted."""

    from hospitals import cms_pos

    sess = cms_pos._session()
    assert sess.headers["Accept"] == "application/json"
    assert "hospitals-kb" in sess.headers["User-Agent"]


def test_a_caller_supplied_accept_header_is_respected():
    import requests

    from hospitals import cms_pos

    custom = requests.Session()
    custom.headers["Accept"] = "application/vnd.api+json"
    assert cms_pos._session(custom).headers["Accept"] == "application/vnd.api+json"


# --- discovery across catalogs --------------------------------------------

DCAT_PAYLOAD = {
    "dataset": [
        {"title": "Something Else", "identifier": "x", "distribution": []},
        {
            "title": "Provider of Services File - Hospital & Non-Hospital Facilities",
            "identifier": "pos-series",
            "modified": "2026-04-01",
            "distribution": [
                {"downloadURL": "https://data.cms.gov/pos-2026-01.csv",
                 "mediaType": "text/csv", "modified": "2026-01-15"},
                {"downloadURL": "https://data.cms.gov/pos-2026-04.csv",
                 "mediaType": "text/csv", "modified": "2026-04-01"},
                {"accessURL": "https://data.cms.gov/pos-dictionary.pdf",
                 "mediaType": "application/pdf"},
            ],
        },
    ]
}

DKAN_PAYLOAD = [
    {
        "title": "Provider of Services File - Hospital & Non-Hospital Facilities",
        "identifier": "pos-series",
        "modified": "2026-04-01",
        "distribution": [
            {"data": {"identifier": "uuid-old", "modified": "2026-01-15"}},
            {"data": {"identifier": "uuid-new", "modified": "2026-04-01"}},
        ],
    }
]

HTML_PAGE = "<!DOCTYPE html><html><title>CMS Data</title>"


def _catalog_session(responses):
    """A session mapping URL -> _FakeResponse."""

    class _S:
        def __init__(self):
            self.headers = {}
            self.seen = []

        def get(self, url, params=None, timeout=None):
            self.seen.append(url)
            return responses[url]

    return _S()


def test_the_dcat_catalog_yields_the_newest_csv():
    from hospitals import cms_pos

    dists = cms_pos._from_dcat(DCAT_PAYLOAD, cms_pos.POS_DATASET_TITLE, "cat")
    assert len(dists) == 2  # the PDF is not a distribution we can read
    assert all(d.distribution_id is None for d in dists)
    assert all(d.has_data_api is False for d in dists)


def test_a_dcat_entry_without_an_id_still_knows_where_the_file_is():
    from hospitals import cms_pos

    import pytest

    d = cms_pos._from_dcat(DCAT_PAYLOAD, cms_pos.POS_DATASET_TITLE, "cat")[0]
    assert d.download_url.endswith(".csv")
    # Asking for a data-api URL it does not have must say so, not build a
    # plausible one ending in "/None/data".
    with pytest.raises(LookupError, match="no data-api identifier"):
        d.data_api_url


def test_discovery_falls_through_a_retired_endpoint_to_a_live_one():
    """CMS retired /api/1/metastore/ — it answers with the portal's own HTML."""

    from hospitals import cms_pos

    retired = "https://data.cms.gov/api/1/metastore/schemas/dataset/items"
    live = "https://data.cms.gov/data.json"
    session = _catalog_session({
        retired: _FakeResponse(200, HTML_PAGE, "text/html"),
        live: _FakeResponse(200, __import__("json").dumps(DCAT_PAYLOAD), "application/json"),
    })

    latest = cms_pos.discover_latest_distribution(
        session=session,
        catalogs=((retired, None, "dkan"), (live, None, "dcat")),
    )

    assert latest.modified == "2026-04-01"
    assert latest.download_url.endswith("pos-2026-04.csv")
    assert session.seen == [retired, live]


def test_a_data_api_catalog_is_preferred_when_it_answers_first():
    from hospitals import cms_pos

    url = "https://example.test/dkan"
    session = _catalog_session(
        {url: _FakeResponse(200, __import__("json").dumps(DKAN_PAYLOAD), "application/json")}
    )

    latest = cms_pos.discover_latest_distribution(
        session=session, catalogs=((url, None, "dkan"),)
    )

    assert latest.distribution_id == "uuid-new"
    assert latest.has_data_api is True
    assert latest.data_api_url.endswith("/uuid-new/data")


def test_every_catalog_tried_is_named_when_none_holds_the_dataset():
    import pytest

    from hospitals import cms_pos

    a, b = "https://example.test/a", "https://example.test/b"
    session = _catalog_session({
        a: _FakeResponse(200, "[]", "application/json"),
        b: _FakeResponse(200, '{"dataset": []}', "application/json"),
    })

    with pytest.raises(LookupError) as excinfo:
        cms_pos.discover_latest_distribution(
            session=session, catalogs=((a, None, "dkan"), (b, None, "dcat"))
        )

    message = str(excinfo.value)
    assert a in message and b in message


def test_unreachable_everywhere_is_reported_as_unavailable_not_missing():
    import pytest

    from hospitals import cms_pos

    a = "https://example.test/a"
    session = _catalog_session({a: _FakeResponse(503, "down", "text/plain")})

    with pytest.raises(cms_pos.CmsUnavailableError, match="no CMS catalog could be reached"):
        cms_pos.discover_latest_distribution(session=session, catalogs=((a, None, "dkan"),))


# --- a renamed dataset ----------------------------------------------------


def test_punctuation_differences_do_not_hide_the_dataset():
    """CMS renders this title several ways; a human reads them as identical."""

    from hospitals import cms_pos

    for title in [
        "Provider of Services File – Hospital & Non-Hospital Facilities",  # en dash
        "Provider of Services File: Hospital & Non-Hospital Facilities",
        "Provider of Services File - Hospital and Non-Hospital Facilities",
        "provider of services file  hospital  non hospital facilities",
    ]:
        items = [{"title": title, "identifier": "x", "distribution": []}]
        assert cms_pos._matching_datasets(items, cms_pos.POS_DATASET_TITLE), title


def test_an_appended_edition_still_matches():
    from hospitals import cms_pos

    items = [{
        "title": "Provider of Services File - Hospital & Non-Hospital Facilities, Q1 2026",
        "identifier": "x",
    }]
    assert cms_pos._matching_datasets(items, cms_pos.POS_DATASET_TITLE)


def test_a_shortened_title_matches_on_the_core_phrase():
    from hospitals import cms_pos

    items = [{"title": "Provider of Services File", "identifier": "x"}]
    assert cms_pos._matching_datasets(items, cms_pos.POS_DATASET_TITLE)


def test_an_exact_match_wins_over_a_looser_one():
    from hospitals import cms_pos

    items = [
        {"title": "Provider of Services File", "identifier": "loose"},
        {"title": cms_pos.POS_DATASET_TITLE, "identifier": "exact"},
    ]
    assert [i["identifier"] for i in cms_pos._matching_datasets(items, cms_pos.POS_DATASET_TITLE)] == ["exact"]


def test_an_unrelated_dataset_is_not_matched():
    from hospitals import cms_pos

    items = [{"title": "Hospital Provider Cost Report", "identifier": "x"}]
    assert cms_pos._matching_datasets(items, cms_pos.POS_DATASET_TITLE) == []


def test_the_failure_names_the_titles_cms_is_actually_publishing():
    """The useful fact is not that the search failed but what it is called now."""

    import json

    import pytest

    from hospitals import cms_pos

    payload = {"dataset": [
        {"title": "Provider of Services File Extract", "identifier": "a",
         "distribution": [{"downloadURL": "x.pdf", "mediaType": "application/pdf"}]},
        {"title": "Medicare Enrollment", "identifier": "b"},
    ]}
    url = "https://example.test/data.json"
    session = _catalog_session({url: _FakeResponse(200, json.dumps(payload), "application/json")})

    with pytest.raises(LookupError) as excinfo:
        cms_pos.discover_latest_distribution(
            session=session,
            dataset_title="Something Nobody Publishes",
            catalogs=((url, None, "dcat"),),
        )

    message = str(excinfo.value)
    assert "Provider of Services File Extract" in message
    assert "Medicare Enrollment" not in message


def test_near_miss_titles_read_both_catalog_shapes():
    from hospitals import cms_pos

    dcat = {"dataset": [{"title": "Provider of Services File Extract"}]}
    dkan = [{"title": "Provider of Services File Extract"}]
    assert cms_pos._near_miss_titles(dcat) == ["Provider of Services File Extract"]
    assert cms_pos._near_miss_titles(dkan) == ["Provider of Services File Extract"]
    assert cms_pos._near_miss_titles("nonsense") == []


def test_a_loosely_matched_title_is_flagged(caplog):
    """CMS renamed the POS file; loading a different dataset must not be quiet."""

    import json
    import logging

    from hospitals import cms_pos

    payload = {"dataset": [{
        "title": "Provider of Services File - Internet Quality Improvement and Evaluation System",
        "identifier": "iqies",
        "modified": "2026-08-14",
        "distribution": [{
            "downloadURL": "https://data.cms.gov/x/POS_File_iQIES_Q4_2025.csv",
            "mediaType": "text/csv",
        }],
    }]}
    url = "https://example.test/data.json"
    session = _catalog_session({url: _FakeResponse(200, json.dumps(payload), "application/json")})

    with caplog.at_level(logging.WARNING):
        latest = cms_pos.discover_latest_distribution(
            session=session, catalogs=((url, None, "dcat"),)
        )

    assert "iQIES" in latest.download_url
    assert any("verify this is the file you want" in r.getMessage() for r in caplog.records)


def test_an_exact_match_says_nothing(caplog):
    import json
    import logging

    from hospitals import cms_pos

    payload = {"dataset": [{
        "title": cms_pos.POS_DATASET_TITLE,
        "identifier": "x",
        "modified": "2026-08-14",
        "distribution": [{"downloadURL": "https://x/pos.csv", "mediaType": "text/csv"}],
    }]}
    url = "https://example.test/data.json"
    session = _catalog_session({url: _FakeResponse(200, json.dumps(payload), "application/json")})

    with caplog.at_level(logging.WARNING):
        cms_pos.discover_latest_distribution(session=session, catalogs=((url, None, "dcat"),))

    assert not [r for r in caplog.records if "verify" in str(r.msg)]


def test_a_csv_only_distribution_is_read_by_downloading_it(monkeypatch):
    """The bug this fixes: ingest demanded a data-api id a DCAT entry has none of."""

    from hospitals import cms_pos

    dist = cms_pos.PosDistribution(
        dataset_id="d", distribution_id=None, title="POS",
        modified="2026-08-14", download_url="https://x/pos.csv",
    )
    monkeypatch.setattr(
        cms_pos, "download_csv", lambda d, session=None: iter([{"STATE_CD": "KS"}])
    )

    assert list(cms_pos.iter_distribution_records(dist)) == [{"STATE_CD": "KS"}]


def test_a_distribution_with_an_id_still_uses_the_data_api(monkeypatch):
    from hospitals import cms_pos

    dist = cms_pos.PosDistribution(
        dataset_id="d", distribution_id="uuid", title="POS",
        modified="2026-08-14", download_url=None,
    )
    monkeypatch.setattr(
        cms_pos,
        "iter_data_api_records",
        lambda d, **kw: iter([{"STATE_CD": "MD"}]),
    )

    assert list(cms_pos.iter_distribution_records(dist)) == [{"STATE_CD": "MD"}]
