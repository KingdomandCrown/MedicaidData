"""Downloading and storing a raw HCRIS cost-report release.

Every vintage (HOSP10FY<year>) is a full cumulative re-publication, and a
report can move from as-submitted to settled between vintages, so nothing is
overwritten: uniqueness is (rpt_rec_num, vintage_year), and a vintage already
holding report rows is skipped outright rather than re-processed.
"""

import io
import os
import zipfile

import pytest
from sqlalchemy import select

from hospitals.db import hcris_alpha, hcris_numeric, hcris_reports, init_db, make_engine
from hospitals.hcris_fetch import HcrisLayoutError, _classify_members, fetch_year

REPORT_LINES = "1001,S1,20260101\r\n1002,S1,20260101\r\n"
# rpt_rec_num, wksht_cd, line_num, clmn_num, value -- 9999 has no matching report.
NUMERIC_LINES = (
    "1001,G000000,1,1,12345.67\r\n"
    "1001,G000000,2,1,999\r\n"
    "1002,G000000,1,1,\r\n"
    "9999,G000000,1,1,111\r\n"
)
ALPHA_LINES = (
    "1001,S200001,100,1,Sample Hospital\r\n"
    "9999,S200001,100,1,Orphan Hospital\r\n"
)


def _zip_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _opener(payload: bytes, calls: list | None = None):
    def opener(url):
        if calls is not None:
            calls.append(url)
        return "application/zip", iter([payload])

    return opener


def _engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'v.sqlite'}")
    init_db(engine)
    return engine


GOOD_ZIP = _zip_bytes(
    {
        "hosp10_2026_RPT.CSV": REPORT_LINES,
        "hosp10_2026_NMRC.CSV": NUMERIC_LINES,
        "hosp10_2026_ALPHA.CSV": ALPHA_LINES,
    }
)


def test_classify_members_finds_each_role_by_keyword():
    roles = _classify_members(["hosp10_2026_RPT.CSV", "hosp10_2026_NMRC.CSV", "hosp10_2026_ALPHA.CSV"])
    assert roles == {
        "report": "hosp10_2026_RPT.CSV",
        "numeric": "hosp10_2026_NMRC.CSV",
        "alpha": "hosp10_2026_ALPHA.CSV",
    }


def test_classify_members_fails_loudly_when_a_role_is_missing():
    with pytest.raises(HcrisLayoutError, match="numeric"):
        _classify_members(["hosp10_2026_RPT.CSV", "hosp10_2026_ALPHA.CSV"])


def test_classify_members_fails_loudly_on_an_ambiguous_role():
    with pytest.raises(HcrisLayoutError, match="report"):
        _classify_members(["a_RPT.CSV", "b_RPT.CSV", "c_NMRC.CSV", "d_ALPHA.CSV"])


def test_a_vintage_loads_reports_numeric_and_alpha_rows(tmp_path):
    engine = _engine(tmp_path)
    calls: list = []

    summary = fetch_year(
        engine, 2026, cache_dir=str(tmp_path / "cache"), opener=_opener(GOOD_ZIP, calls)
    )

    assert summary.status == "loaded"
    assert summary.reports_loaded == 2
    assert summary.numeric_loaded == 3       # the 9999 row is an orphan, not loaded
    assert summary.orphan_numeric == 1
    assert summary.alpha_loaded == 1
    assert summary.orphan_alpha == 1
    assert calls == [
        "https://downloads.cms.gov/files/hcris/HOSP10FY2026.ZIP"
    ]

    with engine.connect() as conn:
        reports = {
            r.rpt_rec_num: r.id
            for r in conn.execute(select(hcris_reports)).all()
        }
        assert set(reports) == {1001, 1002}
        assert all(r.vintage_year == 2026 for r in conn.execute(select(hcris_reports)))

        numeric_rows = {
            (r.report_id, r.wksht_cd, r.line_num, r.clmn_num): r.value
            for r in conn.execute(select(hcris_numeric))
        }
        assert str(numeric_rows[(reports[1001], "G000000", "1", "1")]) == "12345.6700"
        assert str(numeric_rows[(reports[1001], "G000000", "2", "1")]) == "999.0000"
        assert numeric_rows[(reports[1002], "G000000", "1", "1")] is None  # blank value

        alpha_rows = list(conn.execute(select(hcris_alpha)))
        assert len(alpha_rows) == 1
        assert alpha_rows[0].report_id == reports[1001]
        assert alpha_rows[0].value == "Sample Hospital"


def test_a_vintage_already_loaded_is_skipped_without_a_download(tmp_path):
    engine = _engine(tmp_path)
    fetch_year(engine, 2026, cache_dir=str(tmp_path / "cache"), opener=_opener(GOOD_ZIP))

    calls: list = []
    summary = fetch_year(
        engine, 2026, cache_dir=str(tmp_path / "cache"), opener=_opener(GOOD_ZIP, calls)
    )

    assert summary.status == "already_loaded"
    assert summary.reports_loaded == 2
    assert calls == []  # no request was made at all


def test_a_cached_zip_on_disk_is_reused_not_re_downloaded(tmp_path):
    engine = _engine(tmp_path)
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir)
    with open(os.path.join(cache_dir, "HOSP10FY2026.ZIP"), "wb") as f:
        f.write(GOOD_ZIP)

    calls: list = []

    def opener(url):
        calls.append(url)
        raise AssertionError("should not re-download a cached zip")

    summary = fetch_year(engine, 2026, cache_dir=cache_dir, opener=opener)

    assert summary.status == "loaded"
    assert calls == []


def test_two_vintages_of_the_same_report_both_survive(tmp_path):
    """A report revised between releases keeps both copies -- neither vintage
    overwrites the other, so trending never loses a data point."""

    engine = _engine(tmp_path)
    revised_numeric = NUMERIC_LINES.replace("12345.67", "99999.99")
    zip_2027 = _zip_bytes(
        {
            "hosp10_2027_RPT.CSV": REPORT_LINES,
            "hosp10_2027_NMRC.CSV": revised_numeric,
            "hosp10_2027_ALPHA.CSV": ALPHA_LINES,
        }
    )

    fetch_year(engine, 2026, cache_dir=str(tmp_path / "cache"), opener=_opener(GOOD_ZIP))
    fetch_year(engine, 2027, cache_dir=str(tmp_path / "cache"), opener=_opener(zip_2027))

    with engine.connect() as conn:
        vintages = sorted(
            r.vintage_year for r in conn.execute(select(hcris_reports)).all()
        )
    assert vintages == [2026, 2026, 2027, 2027]

    with engine.connect() as conn:
        values = sorted(
            str(r.value)
            for r in conn.execute(
                select(hcris_numeric).where(
                    hcris_numeric.c.wksht_cd == "G000000",
                    hcris_numeric.c.line_num == "1",
                )
            )
        )
    assert "12345.6700" in values
    assert "99999.9900" in values


def test_a_runaway_download_is_stopped_before_it_fills_the_disk(tmp_path):
    engine = _engine(tmp_path)

    def opener(url):
        return "application/zip", (b"x" * 1000 for _ in range(1000))

    with pytest.raises(ValueError, match="exceeded"):
        fetch_year(engine, 2026, cache_dir=str(tmp_path / "cache"), opener=opener, max_bytes=5000)

    assert list((tmp_path / "cache").iterdir()) == []  # no partial file left behind
