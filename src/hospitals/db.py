"""Database schema and loading, portable across SQLite and PostgreSQL.

We use SQLAlchemy Core so the same table definitions and upsert logic run
against both engines. The default target is a local SQLite file; pass a
PostgreSQL URL (``postgresql+psycopg://...``) to load there instead.

Two tables:
  * ``hospitals``       — one row per provider, keyed by normalized CCN.
  * ``ingestion_runs``  — provenance for each ingest (source, state, counts).
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from .logging_config import get_logger
from .normalize import HospitalRecord

log = get_logger(__name__)

metadata = MetaData()

hospitals = Table(
    "hospitals",
    metadata,
    Column("ccn", String(6), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("provider_category_code", String(2)),
    Column("provider_subtype_code", String(4)),
    Column("provider_subtype", String(64)),
    Column("address", String(255)),
    Column("city", String(128)),
    Column("state", String(2), index=True),
    Column("zip5", String(5)),
    Column("county", String(128)),
    Column("phone", String(15)),
    Column("ownership_code", String(4)),
    Column("ownership_type", String(64)),
    Column("certified_bed_count", Integer),
    Column("certification_date", Date),
    Column("termination_code", String(4)),
    Column("is_active", Boolean, index=True),
    Column("ssa_state_code", String(2)),
    Column("fips_state_code", String(2)),
    Column("source", String(64)),
    Column("source_edition", String(128)),
    Column("ingested_at", DateTime),
)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(64), nullable=False),
    Column("state", String(2)),
    Column("source_edition", String(128)),
    Column("records_read", Integer),
    Column("records_loaded", Integer),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("status", String(32)),
    Column("message", String(512)),
)

# Columns that get overwritten on conflict (everything except the PK).
_UPSERT_COLUMNS = [c.name for c in hospitals.columns if c.name != "ccn"]


@dataclass
class LoadResult:
    loaded: int
    edition: str | None


def make_engine(database_url: str, echo: bool = False) -> Engine:
    """Create an engine, ensuring a SQLite file's parent directory exists."""

    _ensure_sqlite_dir(database_url)
    log.info("Connecting to database: %s", _redact(database_url))
    return create_engine(database_url, echo=echo, future=True)


def _ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = database_url[len(prefix) :]
    if not path or path == ":memory:":
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def _redact(url: str) -> str:
    """Hide credentials in a SQLAlchemy URL for logging."""

    if "@" in url and "//" in url:
        scheme, rest = url.split("//", 1)
        if "@" in rest:
            _creds, host = rest.split("@", 1)
            return f"{scheme}//***@{host}"
    return url


def init_db(engine: Engine) -> None:
    """Create tables if they do not exist."""

    metadata.create_all(engine)
    log.info("Ensured schema (tables: %s)", ", ".join(metadata.tables))


def _row_from_record(
    record: HospitalRecord,
    source: str,
    edition: str | None,
    now: dt.datetime,
) -> dict:
    row = record.as_dict()
    row["source"] = source
    row["source_edition"] = edition
    row["ingested_at"] = now
    return row


def upsert_hospitals(
    engine: Engine,
    records: Sequence[HospitalRecord],
    source: str,
    edition: str | None,
    batch_size: int = 500,
) -> int:
    """Insert or update hospitals keyed by CCN. Returns the number written.

    Uses native ``ON CONFLICT`` upserts on SQLite and PostgreSQL and falls
    back to delete-then-insert on any other dialect.
    """

    if not records:
        return 0

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows = [_row_from_record(r, source, edition, now) for r in records if r.ccn]
    if not rows:
        return 0

    dialect = engine.dialect.name
    with engine.begin() as conn:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            _write_batch(conn, batch, dialect)
    log.info("Upserted %d hospital rows (%s)", len(rows), dialect)
    return len(rows)


def _write_batch(conn, batch: list[dict], dialect: str) -> None:
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(hospitals)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ccn"],
            set_={c: stmt.excluded[c] for c in _UPSERT_COLUMNS},
        )
        conn.execute(stmt, batch)
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(hospitals)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ccn"],
            set_={c: stmt.excluded[c] for c in _UPSERT_COLUMNS},
        )
        conn.execute(stmt, batch)
    else:
        # Portable fallback: delete existing PKs, then bulk insert.
        ccns = [r["ccn"] for r in batch]
        conn.execute(hospitals.delete().where(hospitals.c.ccn.in_(ccns)))
        conn.execute(insert(hospitals), batch)


def record_run(
    engine: Engine,
    *,
    source: str,
    state: str | None,
    edition: str | None,
    records_read: int,
    records_loaded: int,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    status: str,
    message: str | None = None,
) -> None:
    """Write a provenance row to ``ingestion_runs``."""

    with engine.begin() as conn:
        conn.execute(
            insert(ingestion_runs),
            {
                "source": source,
                "state": state,
                "source_edition": edition,
                "records_read": records_read,
                "records_loaded": records_loaded,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "message": (message or "")[:512] or None,
            },
        )


def count_hospitals(engine: Engine, state: str | None = None) -> int:
    stmt = select(func.count()).select_from(hospitals)
    if state:
        stmt = stmt.where(hospitals.c.state == state.upper())
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())
