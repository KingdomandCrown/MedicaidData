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
