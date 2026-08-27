"""Choosing who to crawl.

The counts this produces are the honest ceiling on what any crawler can do:
a hospital with no website on record cannot be reached by any amount of
cleverness, and that number should be visible before a run rather than
discovered as a pile of failures after one.
"""

import datetime as dt
import json

import pytest
from sqlalchemy import insert

from hospitals.db import charge_sources, hospitals, init_db, make_engine
from hospitals.mrf_targets import choose_targets, load_websites

NOW = dt.datetime(2026, 8, 27)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 't.sqlite'}")
    init_db(eng)
    return eng


def _hospital(engine, ccn, name, state):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            dict(ccn=ccn, name=name, state=state, is_active=True, ingested_at=NOW),
        )


def _covered(engine, ccn):
    with engine.begin() as conn:
        conn.execute(
            insert(charge_sources),
            dict(source_file=f"{ccn}.json", ccn=ccn, charge_count=1, ingested_at=NOW),
        )


# --- reading the website file ---------------------------------------------


def test_websites_are_read_from_the_scorecard_profile(tmp_path):
    path = tmp_path / "hospital-info.json"
    path.write_text(json.dumps({"hospitals": {
        "170027": {"name": "PRATT", "web": "https://prmc.org"},
        "170045": {"name": "ST FRANCIS", "web": "https://vc.org"},
    }}))

    assert load_websites(str(path)) == {
        "170027": "https://prmc.org",
        "170045": "https://vc.org",
    }


def test_a_hospital_with_no_website_is_simply_absent(tmp_path):
    path = tmp_path / "i.json"
    path.write_text(json.dumps({"hospitals": {"170027": {"name": "PRATT"}}}))
    assert load_websites(str(path)) == {}


def test_the_mapping_may_be_the_whole_file(tmp_path):
    """The file has had both shapes across versions."""

    path = tmp_path / "i.json"
    path.write_text(json.dumps({"170027": {"web": "https://prmc.org"}}))
    assert load_websites(str(path)) == {"170027": "https://prmc.org"}


def test_a_junk_entry_does_not_stop_the_load(tmp_path):
    path = tmp_path / "i.json"
    path.write_text(json.dumps({"hospitals": {
        "170027": {"web": "https://prmc.org"},
        "_note": "not a hospital",
        "170045": None,
    }}))
    assert load_websites(str(path)) == {"170027": "https://prmc.org"}


# --- choosing ---------------------------------------------------------------


WEB = {"170027": "https://prmc.org", "170045": "https://vc.org", "260001": "https://mo.org"}


def test_only_hospitals_without_a_file_are_targeted(engine):
    """This fills the hole; it does not re-download what we hold."""

    _hospital(engine, "170027", "PRATT", "KS")
    _hospital(engine, "170045", "ST FRANCIS", "KS")
    _covered(engine, "170045")

    summary = choose_targets(engine, WEB)

    assert [t.ccn for t in summary.targets] == ["170027"]
    assert summary.already_covered == 1


def test_a_state_filter_narrows_the_run(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _hospital(engine, "260001", "MISSOURI GENERAL", "MO")

    assert [t.ccn for t in choose_targets(engine, WEB, states=["KS"]).targets] == ["170027"]
    assert len(choose_targets(engine, WEB, states=["KS", "MO"]).targets) == 2


def test_a_lowercase_state_is_accepted(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    assert len(choose_targets(engine, WEB, states=["ks"]).targets) == 1


def test_hospitals_with_no_website_are_counted_not_dropped_silently(engine):
    """That count is the ceiling on what any crawler can ever reach."""

    _hospital(engine, "170027", "PRATT", "KS")
    _hospital(engine, "170099", "NO WEBSITE MEMORIAL", "KS")

    summary = choose_targets(engine, WEB)

    assert summary.no_website == 1
    assert summary.in_scope == 2
    assert len(summary.targets) == 1


def test_the_counts_account_for_every_hospital_in_scope(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _hospital(engine, "170045", "ST FRANCIS", "KS")
    _hospital(engine, "170099", "NO WEBSITE MEMORIAL", "KS")
    _covered(engine, "170045")

    s = choose_targets(engine, WEB)
    assert s.in_scope == s.already_covered + s.no_website + len(s.targets)


def test_a_limit_caps_the_run(engine):
    for n in range(5):
        _hospital(engine, f"17002{n}", f"H{n}", "KS")
    web = {f"17002{n}": "https://x.org" for n in range(5)}

    assert len(choose_targets(engine, web, limit=2).targets) == 2


def test_covered_hospitals_can_be_included_deliberately(engine):
    """For refreshing a file that has gone stale, rather than filling a gap."""

    _hospital(engine, "170045", "ST FRANCIS", "KS")
    _covered(engine, "170045")

    assert len(choose_targets(engine, WEB, include_covered=True).targets) == 1


def test_an_empty_database_asks_for_nothing(engine):
    summary = choose_targets(engine, WEB)
    assert summary.targets == []
    assert summary.in_scope == 0


def test_targets_carry_what_discovery_needs(engine):
    _hospital(engine, "170027", "PRATT REGIONAL MEDICAL CENTER", "KS")
    target = choose_targets(engine, WEB).targets[0].as_dict()

    assert set(target) == {"ccn", "name", "state", "website"}
    assert target["website"] == "https://prmc.org"
