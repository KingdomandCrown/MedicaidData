"""Journal mode: the pragma that keeps WAL's index out of a mapped file.

A bus error killed a 580 GB load mid-commit on an external volume. The crash
report faulted inside ``sqlite3WalCheckpoint`` with "FS pagein error: 22" — the
``-shm`` file SQLite memory maps for its WAL index. A rollback journal creates
no such file, so nothing can fault. WAL stays the default; the volume that
cannot take it asks for something else.
"""

import os

from hospitals import db


def _pragma(engine, name):
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_wal_is_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HOSPITALS_SQLITE_JOURNAL", raising=False)
    engine = db.make_engine(f"sqlite:///{tmp_path/'a.sqlite'}")
    assert _pragma(engine, "journal_mode") == "wal"


def test_truncate_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSPITALS_SQLITE_JOURNAL", "truncate")
    engine = db.make_engine(f"sqlite:///{tmp_path/'b.sqlite'}")
    assert _pragma(engine, "journal_mode") == "truncate"


def test_nonsense_falls_back_to_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSPITALS_SQLITE_JOURNAL", "sideways")
    engine = db.make_engine(f"sqlite:///{tmp_path/'c.sqlite'}")
    assert _pragma(engine, "journal_mode") == "wal"


def test_truncate_leaves_no_shm_file(tmp_path, monkeypatch):
    """The whole point: no -shm file means nothing to memory map."""

    monkeypatch.setenv("HOSPITALS_SQLITE_JOURNAL", "truncate")
    path = tmp_path / "d.sqlite"
    engine = db.make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (x integer)")
        conn.exec_driver_sql("INSERT INTO t VALUES (1)")
    assert not os.path.exists(f"{path}-shm")


def test_two_connections_can_still_write(tmp_path, monkeypatch):
    """Exclusive locking deadlocked the CLI against its own second engine."""

    monkeypatch.setenv("HOSPITALS_SQLITE_JOURNAL", "truncate")
    url = f"sqlite:///{tmp_path/'e.sqlite'}"
    first = db.make_engine(url)
    with first.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (x integer)")
    second = db.make_engine(url)
    with second.begin() as conn:
        conn.exec_driver_sql("INSERT INTO t VALUES (2)")
    with first.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM t").scalar() == 1
