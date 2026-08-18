"""Building the NPI -> CCN crosswalk from CMS Hospital Enrollments.

Without it the linker matched 281 of 835 charge files by name, leaving 549
files and 632 million charge rows attached to no hospital — because the biggest
publishers name a *system*: dignity-health, atrium-health-hospitals-inc,
the-charlotte-mecklenburg-hospital-authority. There is no hospital called
Dignity Health. There are twenty, and only the NPI tells them apart.
"""

import datetime as dt

import pytest
from sqlalchemy import insert, select

from hospitals import cms_pos, crosswalk
from hospitals.db import charge_sources, hospitals, init_db, make_engine, npi_ccn_crosswalk
from hospitals.link import link_charges

NOW = dt.datetime(2026, 8, 18)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'x.sqlite'}")
    init_db(eng)
    return eng


DIST = cms_pos.PosDistribution(
    dataset_id="he",
    distribution_id=None,
    title="Hospital Enrollments",
    modified="2026-08-17",
    download_url="https://data.cms.gov/hospital-enrollments.csv",
)


def _serve(monkeypatch, rows, distribution=DIST):
    monkeypatch.setattr(
        crosswalk.cms_pos, "discover_latest_distribution",
        lambda session=None, dataset_title=None: distribution,
    )
    monkeypatch.setattr(
        crosswalk.cms_pos, "iter_distribution_records",
        lambda dist, session=None, **kw: iter(rows),
    )


def _pairs(engine):
    with engine.connect() as conn:
        return {
            r.npi: r.ccn
            for r in conn.execute(select(npi_ccn_crosswalk.c.npi, npi_ccn_crosswalk.c.ccn))
        }


# --- reading the file -----------------------------------------------------


def test_npi_and_ccn_are_loaded(engine, monkeypatch):
    _serve(monkeypatch, [
        {"NPI": "1669348991", "CCN": "340001", "ORGANIZATION_NAME": "ATRIUM HEALTH"},
        {"NPI": "1770626426", "CCN": "050100", "ORGANIZATION_NAME": "DIGNITY HEALTH"},
    ])

    summary = crosswalk.fetch_crosswalk(engine)

    assert summary.loaded == 2
    assert summary.rows_read == 2
    assert _pairs(engine) == {"1669348991": "340001", "1770626426": "050100"}
    assert summary.source_title == "Hospital Enrollments"


@pytest.mark.parametrize(
    "npi_key,ccn_key",
    [
        ("NPI", "CCN"),
        ("npi", "ccn"),
        ("NPI_NUM", "MEDICARE_CCN"),
        ("NPI Number", "Provider Number"),
    ],
)
def test_columns_are_found_under_any_spelling_cms_uses(engine, monkeypatch, npi_key, ccn_key):
    _serve(monkeypatch, [{npi_key: "1669348991", ccn_key: "340001"}])
    assert crosswalk.fetch_crosswalk(engine).loaded == 1


def test_a_file_without_the_two_identifiers_fails_loudly(engine, monkeypatch):
    _serve(monkeypatch, [{"ORGANIZATION_NAME": "SOMEWHERE", "STATE": "KS"}])
    with pytest.raises(LookupError, match="no NPI/CCN columns"):
        crosswalk.fetch_crosswalk(engine)


def test_rows_missing_either_identifier_are_skipped(engine, monkeypatch):
    _serve(monkeypatch, [
        {"NPI": "1669348991", "CCN": "340001"},
        {"NPI": "", "CCN": "340002"},
        {"NPI": "1770626426", "CCN": ""},
    ])

    summary = crosswalk.fetch_crosswalk(engine)
    assert summary.rows_read == 3
    assert summary.loaded == 1


def test_the_organization_name_is_kept_when_present(engine, monkeypatch):
    _serve(monkeypatch, [{"NPI": "1", "CCN": "340001", "DOING_BUSINESS_AS_NAME": "ATRIUM"}])
    crosswalk.fetch_crosswalk(engine)
    with engine.connect() as conn:
        assert conn.execute(select(npi_ccn_crosswalk.c.name)).scalar_one() == "ATRIUM"


# --- one NPI, several enrollments -----------------------------------------


def test_the_first_ccn_wins_and_the_disagreement_is_counted(engine, monkeypatch):
    """A hospital can hold several enrollments; npi is the primary key.

    Keeping the last would make the result depend on row order.
    """

    _serve(monkeypatch, [
        {"NPI": "1669348991", "CCN": "340001"},
        {"NPI": "1669348991", "CCN": "340999"},
    ])

    summary = crosswalk.fetch_crosswalk(engine)
    assert summary.loaded == 1
    assert summary.conflict_count == 1
    assert _pairs(engine) == {"1669348991": "340001"}


def test_a_repeated_row_is_not_a_conflict(engine, monkeypatch):
    _serve(monkeypatch, [
        {"NPI": "1669348991", "CCN": "340001"},
        {"NPI": "1669348991", "CCN": "340001"},
    ])
    assert crosswalk.fetch_crosswalk(engine).conflict_count == 0


def test_refetching_replaces_rather_than_duplicates(engine, monkeypatch):
    _serve(monkeypatch, [{"NPI": "1669348991", "CCN": "340001"}])
    crosswalk.fetch_crosswalk(engine)

    _serve(monkeypatch, [{"NPI": "1669348991", "CCN": "340777"}])
    crosswalk.fetch_crosswalk(engine)

    assert _pairs(engine) == {"1669348991": "340777"}


# --- the point of all this ------------------------------------------------


def test_a_system_named_file_links_once_the_crosswalk_exists(engine, monkeypatch):
    """dignity-health matches no hospital by name; its NPI matches exactly one."""

    with engine.begin() as conn:
        conn.execute(insert(hospitals), {
            "ccn": "050100", "name": "MERCY GENERAL HOSPITAL", "state": "CA",
            "is_active": True, "ingested_at": NOW,
        })
        conn.execute(insert(charge_sources), {
            "source_file": "941196203-1770626426_dignity-health_standardcharges.json",
            "hospital_name": "DIGNITY HEALTH",
            "primary_npi": "1770626426",
            "license_state": "CA",
            "charge_count": 370062,
            "ingested_at": NOW,
        })

    before = link_charges(engine)
    assert before.by_crosswalk == 0
    assert before.unlinked == 1  # no hospital is called "Dignity Health"

    _serve(monkeypatch, [{"NPI": "1770626426", "CCN": "050100"}])
    crosswalk.fetch_crosswalk(engine)

    after = link_charges(engine)
    assert after.by_crosswalk == 1
    assert after.unlinked == 0

    with engine.connect() as conn:
        row = conn.execute(
            select(charge_sources.c.ccn, charge_sources.c.link_method)
        ).one()
    assert row.ccn == "050100"
    assert row.link_method == "crosswalk_npi"
