from sqlalchemy import select

from hospitals.db import hospitals, ingestion_runs, make_engine
from hospitals.ingest import ingest_state


def test_end_to_end_kansas_from_fixture(tmp_path, fixture_csv):
    db_url = f"sqlite:///{tmp_path / 'ks.sqlite'}"
    summary = ingest_state(
        state="KS",
        database_url=db_url,
        input_file=fixture_csv,
        active_only=True,
    )

    # Fixture has 7 rows: 5 KS hospitals (1 terminated), 1 KS SNF, 1 MD hospital.
    assert summary.state == "KS"
    assert summary.records_read == 7
    assert summary.hospitals_matched == 5  # KS, category 01 (incl. terminated)
    assert summary.active_matched == 4
    assert summary.loaded == 4  # active_only drops the terminated one

    engine = make_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(hospitals.c.ccn, hospitals.c.name, hospitals.c.state).order_by(
                hospitals.c.ccn
            )
        ).all()
    ccns = [r.ccn for r in rows]
    assert ccns == ["170012", "170045", "170089", "171300"]
    assert all(r.state == "KS" for r in rows)

    # The Maryland hospital and the Kansas SNF must not be present.
    with engine.connect() as conn:
        all_states = {
            r.state for r in conn.execute(select(hospitals.c.state)).all()
        }
    assert all_states == {"KS"}


def test_include_inactive_keeps_terminated(tmp_path, fixture_csv):
    db_url = f"sqlite:///{tmp_path / 'ks_all.sqlite'}"
    summary = ingest_state(
        state="KS",
        database_url=db_url,
        input_file=fixture_csv,
        active_only=False,
    )
    assert summary.loaded == 5  # includes the terminated hospital

    engine = make_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(hospitals.c.is_active).where(hospitals.c.ccn == "170777")
        ).scalar_one()
    assert row is False


def test_ingest_is_idempotent(tmp_path, fixture_csv):
    db_url = f"sqlite:///{tmp_path / 'idem.sqlite'}"
    ingest_state(state="KS", database_url=db_url, input_file=fixture_csv)
    ingest_state(state="KS", database_url=db_url, input_file=fixture_csv)

    engine = make_engine(db_url)
    with engine.connect() as conn:
        count = conn.execute(select(hospitals.c.ccn)).all()
        runs = conn.execute(select(ingestion_runs.c.id)).all()
    assert len(count) == 4  # upsert, not duplicate
    assert len(runs) == 2  # two provenance rows recorded


def test_maryland_filter_from_same_fixture(tmp_path, fixture_csv):
    db_url = f"sqlite:///{tmp_path / 'md.sqlite'}"
    summary = ingest_state(
        state="MD",
        database_url=db_url,
        input_file=fixture_csv,
        active_only=True,
    )
    assert summary.loaded == 1
    engine = make_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(hospitals.c.ccn, hospitals.c.state)
        ).one()
    assert row.ccn == "210001"
    assert row.state == "MD"


def test_all_states_loads_every_state_in_one_pass(tmp_path, fixture_csv):
    """--state ALL is the national load that feeds the gap report."""

    db_url = f"sqlite:///{tmp_path / 'all.sqlite'}"
    summary = ingest_state(
        state="ALL",
        database_url=db_url,
        input_file=fixture_csv,
        active_only=True,
    )
    assert summary.state == "ALL"

    engine = make_engine(db_url)
    with engine.connect() as conn:
        states = {r[0] for r in conn.execute(select(hospitals.c.state))}
    # Both states in the fixture arrive from a single read.
    assert {"KS", "MD"} <= states
    assert summary.loaded == len(states) or summary.loaded >= 2
