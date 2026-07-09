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
