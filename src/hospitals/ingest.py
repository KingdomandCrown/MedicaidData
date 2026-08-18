"""End-to-end ingestion orchestration.

Ties the pieces together: fetch raw POS records -> filter to active hospitals
in the target state -> normalize -> load into the database, recording a
provenance row for the run.

The pipeline is state-parameterized. Kansas is the default; Maryland (and any
other state in :mod:`hospitals.states`) works by passing a different code.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass
from typing import Iterable

from . import cms_pos, normalize
from .db import init_db, make_engine, record_run, upsert_hospitals
from .logging_config import get_logger
from .states import resolve_state

log = get_logger(__name__)

SOURCE_NAME = "cms_pos"

# Accepted spellings for "load every state in one pass".
_ALL_STATES = {"ALL", "US", "USA", "NATIONAL"}


@dataclass
class IngestSummary:
    state: str
    records_read: int
    hospitals_matched: int
    active_matched: int
    loaded: int
    edition: str | None
    status: str


def _filter_and_normalize(
    raw_records: Iterable[dict],
    state_usps: str | None,
    active_only: bool,
) -> tuple[list[normalize.HospitalRecord], int, int, int]:
    """Filter raw records to the state's hospitals and normalize them.

    Returns ``(records, read, hospitals_matched, active_matched)``.

    We re-check the state and hospital category client-side even when the
    data-api already filtered, so the local-file path (which is unfiltered)
    produces identical results and any API quirks are caught. ``state_usps``
    of ``None`` keeps every state (the national load).
    """

    target = state_usps.upper() if state_usps else None
    read = 0
    hospitals_matched = 0
    active_matched = 0
    out: list[normalize.HospitalRecord] = []

    for raw in raw_records:
        read += 1
        if not normalize.is_hospital(raw):
            continue
        record = normalize.normalize_record(raw)
        if target is not None and record.state != target:
            continue
        hospitals_matched += 1
        if record.is_active:
            active_matched += 1
        elif active_only:
            continue
        if not record.ccn:
            log.warning("Skipping hospital with no CCN: %r", record.name)
            continue
        out.append(record)

    return out, read, hospitals_matched, active_matched


def ingest_state(
    state: str = "KS",
    *,
    database_url: str = "sqlite:///data/hospitals.sqlite",
    input_file: str | None = None,
    active_only: bool = True,
    limit: int | None = None,
    echo_sql: bool = False,
) -> IngestSummary:
    """Ingest hospitals for one state from the CMS POS file.

    Parameters
    ----------
    state:
        USPS abbreviation or full name (e.g. ``"KS"`` / ``"Kansas"``).
    database_url:
        SQLAlchemy URL. Defaults to a local SQLite file. Use a
        ``postgresql+psycopg://`` URL to load into PostgreSQL.
    input_file:
        Optional path to a local POS CSV (offline path). When omitted, the
        latest file is discovered and streamed from CMS.
    active_only:
        Keep only providers whose termination code marks them active.
    limit:
        Cap on raw records read (useful for smoke tests).
    """

    # "ALL" loads every state in a single pass over the national file, rather
    # than downloading it once per state.
    nationwide = state.strip().upper() in _ALL_STATES
    state_obj = None if nationwide else resolve_state(state)
    target = None if nationwide else state_obj.usps
    label = "all states" if nationwide else f"{state_obj.name} ({state_obj.usps})"

    started_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    log.info("Starting CMS POS ingestion for %s; active_only=%s", label, active_only)

    engine = make_engine(database_url, echo=echo_sql)
    init_db(engine)

    edition: str | None = input_file
    status = "success"
    message = None
    records: list[normalize.HospitalRecord] = []
    read = hospitals_matched = active_matched = 0

    try:
        # For the CMS path, resolve the edition label up front for provenance.
        if not input_file:
            try:
                dist = cms_pos.discover_latest_distribution()
                edition = f"{dist.title} ({dist.modified})"
                raw = cms_pos.iter_distribution_records(
                    dist,
                    state_usps=target,
                    hospitals_only=True,
                )
                if limit is not None:
                    raw = itertools.islice(raw, limit)
            except cms_pos.CmsUnavailableError:
                raise
        else:
            raw = cms_pos.fetch_records(
                state_usps=target,
                input_file=input_file,
                limit=limit,
            )

        records, read, hospitals_matched, active_matched = _filter_and_normalize(
            raw, target, active_only
        )
        loaded = upsert_hospitals(
            engine, records, source=SOURCE_NAME, edition=edition
        )
    except cms_pos.CmsUnavailableError as exc:
        status = "failed"
        message = str(exc)
        loaded = 0
        log.error("CMS unavailable: %s", exc)
    finally:
        finished_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        record_run(
            engine,
            source=SOURCE_NAME,
            state=target,
            edition=edition,
            records_read=read,
            records_loaded=len(records),
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            message=message,
        )

    if status == "success":
        log.info(
            "Done: read=%d, hospitals=%d, active=%d, loaded=%d for %s",
            read,
            hospitals_matched,
            active_matched,
            loaded,
            label,
        )
    else:
        # Surface the failure to the caller.
        raise cms_pos.CmsUnavailableError(message or "ingestion failed")

    return IngestSummary(
        state=target or "ALL",
        records_read=read,
        hospitals_matched=hospitals_matched,
        active_matched=active_matched,
        loaded=loaded,
        edition=edition,
        status=status,
    )
