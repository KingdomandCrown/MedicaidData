"""Reading a database while a long ingest is writing to it.

A multi-hour batch holds the write lock in bursts. Out of the box that makes
every other command — coverage, tidying a folder — fail outright with
"database is locked", which is how a routine check killed a running batch's
sibling command mid-load.
"""

import datetime as dt

from sqlalchemy import insert, text

from hospitals.db import charge_sources, init_db, loaded_source_files, make_engine

NOW = dt.datetime(2026, 8, 17)


def test_sqlite_is_configured_for_concurrent_reads(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'c.sqlite'}")
    init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() >= 30_000


def test_a_reader_works_while_a_writer_holds_a_transaction(tmp_path):
    """The exact failure: a read arriving mid-write must not be refused."""

    url = f"sqlite:///{tmp_path / 'c.sqlite'}"
    writer = make_engine(url)
    init_db(writer)

    with writer.begin() as write_conn:
        write_conn.execute(
            insert(charge_sources),
            [dict(source_file="mid_batch.csv", charge_count=5, ingested_at=NOW)],
        )
        # A separate connection reads while that transaction is still open.
        reader = make_engine(url)
        assert loaded_source_files(reader) == set()  # not yet committed, but no error

    assert loaded_source_files(make_engine(url)) == {"mid_batch.csv"}


def test_a_postgres_url_is_left_alone(tmp_path, monkeypatch):
    """The pragmas are SQLite-only and must not be attempted elsewhere."""

    import hospitals.db as db

    called = []
    monkeypatch.setattr(db, "_configure_sqlite", lambda e: called.append(e))
    # Stub the engine factory so this needs no PostgreSQL driver installed.
    monkeypatch.setattr(db, "create_engine", lambda url, **kw: object())

    db.make_engine(f"sqlite:///{tmp_path / 'x.sqlite'}")
    assert len(called) == 1

    db.make_engine("postgresql+psycopg://u:p@localhost/none")
    assert len(called) == 1, "PostgreSQL must not get the SQLite pragmas"
