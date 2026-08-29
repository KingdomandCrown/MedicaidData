"""Writing down a decision no inference could make.

144 files hold 158 million charge rows attributed to nobody, and a fresh CMS
crosswalk moved none of them — those files carry no NPI CMS has ever heard of.
They are not a download problem. They are on the disk, parsed, waiting for one
column to be filled in, and there was no way to fill it.

The tests that matter are the refusals. A wrong attribution here cannot be seen
afterwards: the hospital simply appears to have prices, and they are somebody
else's.
"""

import datetime as dt

import pytest
from sqlalchemy import insert, select

from hospitals.assign import (
    MANUAL,
    apply_links,
    read_suggestions,
    suggest_rows,
    write_suggestions,
)
from hospitals.db import charge_sources, hospitals, init_db, make_engine

NOW = dt.datetime(2026, 8, 29)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'a.sqlite'}")
    init_db(eng)
    return eng


def _hospital(engine, ccn, name, state="MO"):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            dict(ccn=ccn, name=name, state=state, is_active=True, ingested_at=NOW),
        )


def _source(engine, source_file, *, name=None, ccn=None, method=None, count=1000):
    with engine.begin() as conn:
        conn.execute(
            insert(charge_sources),
            dict(source_file=source_file, hospital_name=name, ccn=ccn,
                 link_method=method, charge_count=count, ingested_at=NOW),
        )


def _linked(engine, source_file):
    with engine.connect() as conn:
        return conn.execute(
            select(charge_sources.c.ccn, charge_sources.c.link_method).where(
                charge_sources.c.source_file == source_file
            )
        ).one()


def _row(source_file, ccn, confirm="y"):
    return {"source_file": source_file, "ccn": ccn, "confirm": confirm}


# --- the decision gets recorded -------------------------------------------


def test_a_confirmed_decision_is_written(engine):
    _hospital(engine, "260020", "MERCY HOSPITAL ST LOUIS")
    _source(engine, "orphan.json")

    summary = apply_links(engine, [_row("orphan.json", "260020")], dry_run=False)

    assert summary.applied == 1
    assert _linked(engine, "orphan.json") == ("260020", MANUAL)


def test_the_method_says_a_person_decided(engine):
    """"How do we know this?" has a different answer here than for a join."""

    _hospital(engine, "260020", "MERCY")
    _source(engine, "orphan.json")
    apply_links(engine, [_row("orphan.json", "260020")], dry_run=False)

    assert _linked(engine, "orphan.json").link_method == MANUAL
    assert MANUAL not in ("crosswalk_npi", "name_state", "filename_ccn")


@pytest.mark.parametrize("confirm", ["y", "Y", "yes", "YES", "1", "x", "true", "ok"])
def test_confirmation_is_read_however_it_was_typed(engine, confirm):
    _hospital(engine, "260020", "MERCY")
    _source(engine, "orphan.json")

    assert apply_links(engine, [_row("orphan.json", "260020", confirm)],
                       dry_run=False).applied == 1


# --- nothing happens without being asked twice ----------------------------


def test_a_dry_run_writes_nothing(engine):
    """The default, because a wrong attribution is invisible afterwards."""

    _hospital(engine, "260020", "MERCY")
    _source(engine, "orphan.json")

    summary = apply_links(engine, [_row("orphan.json", "260020")])

    assert summary.applied == 1
    assert _linked(engine, "orphan.json").ccn is None


def test_an_unconfirmed_row_is_left_alone(engine):
    _hospital(engine, "260020", "MERCY")
    _source(engine, "orphan.json")

    summary = apply_links(engine, [_row("orphan.json", "260020", "")], dry_run=False)

    assert summary.skipped == 1
    assert summary.applied == 0
    assert _linked(engine, "orphan.json").ccn is None


def test_a_maybe_is_not_a_yes(engine):
    _hospital(engine, "260020", "MERCY")
    _source(engine, "orphan.json")

    summary = apply_links(engine, [_row("orphan.json", "260020", "maybe")], dry_run=False)
    assert summary.applied == 0


# --- the refusals ---------------------------------------------------------


def test_a_ccn_no_hospital_has_is_refused(engine):
    """A typo in six digits would silently invent a hospital's prices."""

    _hospital(engine, "260020", "MERCY")
    _source(engine, "orphan.json")

    summary = apply_links(engine, [_row("orphan.json", "269999")], dry_run=False)

    assert summary.refused == 1
    assert summary.problems[0].status == "unknown_ccn"
    assert _linked(engine, "orphan.json").ccn is None


def test_a_file_the_database_does_not_hold_is_refused(engine):
    _hospital(engine, "260020", "MERCY")

    summary = apply_links(engine, [_row("not-here.json", "260020")], dry_run=False)

    assert summary.refused == 1
    assert summary.problems[0].status == "unknown_file"


def test_an_already_linked_file_is_not_quietly_reattributed(engine):
    """Overwriting an inferred link with a worse guess is a real risk."""

    _hospital(engine, "260020", "MERCY ST LOUIS")
    _hospital(engine, "260001", "MERCY JOPLIN")
    _source(engine, "linked.json", ccn="260001", method="crosswalk_npi")

    summary = apply_links(engine, [_row("linked.json", "260020")], dry_run=False)

    assert summary.refused == 1
    assert summary.problems[0].status == "already_linked"
    assert _linked(engine, "linked.json").ccn == "260001"


def test_relinking_is_possible_when_asked_for(engine):
    _hospital(engine, "260020", "MERCY ST LOUIS")
    _hospital(engine, "260001", "MERCY JOPLIN")
    _source(engine, "linked.json", ccn="260001", method="name_state")

    apply_links(engine, [_row("linked.json", "260020")], dry_run=False, relink=True)

    assert _linked(engine, "linked.json") == ("260020", MANUAL)


def test_confirming_the_link_a_file_already_has_is_not_a_conflict(engine):
    _hospital(engine, "260020", "MERCY")
    _source(engine, "linked.json", ccn="260020", method="name_state")

    summary = apply_links(engine, [_row("linked.json", "260020")], dry_run=False)

    assert summary.applied == 1
    assert _linked(engine, "linked.json").link_method == MANUAL


def test_every_row_is_accounted_for(engine):
    _hospital(engine, "260020", "MERCY")
    _source(engine, "a.json")

    summary = apply_links(
        engine,
        [_row("a.json", "260020"), _row("a.json", "260020", ""), _row("b.json", "260020")],
        dry_run=False,
    )

    assert summary.rows == 3
    assert summary.applied + summary.skipped + summary.refused == 3


# --- the review sheet -----------------------------------------------------


def test_the_biggest_files_are_decided_on_first(engine):
    """A file holding five million rows deserves attention nine thousand does not."""

    _hospital(engine, "260020", "MERCY HOSPITAL ST LOUIS")
    _hospital(engine, "351320", "MERCY MEDICAL CENTER WILLISTON", state="ND")
    _source(engine, "mercy-hospital-st-louis.json", count=5_440_625)
    _source(engine, "mercy-medical-center-williston.xlsx", count=9_702)

    rows = suggest_rows(engine)

    assert rows[0]["charge_rows"] == 5_440_625
    assert rows[0]["confirm"] == ""


def test_a_suggestion_sheet_round_trips(engine, tmp_path):
    _hospital(engine, "260020", "MERCY HOSPITAL ST LOUIS")
    _source(engine, "mercy-hospital-st-louis.json", count=100)

    path = str(tmp_path / "suggestions.csv")
    write_suggestions(suggest_rows(engine), path)
    back = read_suggestions(path)

    assert back[0]["ccn"] == "260020"
    assert back[0]["confirm"] == ""

    back[0]["confirm"] = "y"
    assert apply_links(engine, back, dry_run=False).applied == 1


def test_a_reviewer_can_be_given_only_the_top_of_the_list(engine):
    for n in range(5):
        _hospital(engine, f"26002{n}", f"MERCY HOSPITAL {n}")
        _source(engine, f"mercy-hospital-{n}.json", count=n * 1000)

    assert len(suggest_rows(engine, limit=2)) == 2
