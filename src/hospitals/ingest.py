"""End-to-end ingestion orchestration.

Ties the pieces together: fetch raw POS records -> filter to active hospitals
in the target state -> normalize -> load into the database, recording a
provenance row for the run.

The pipeline is state-parameterized. Kansas is the default; Maryland (and any
other state in :mod:`hospitals.states`) works by passing a different code.
Passing ``"ALL"`` (or ``"US"`` / ``"USA"`` / ``"NATIONAL"``) skips the state
filter entirely and ingests every US hospital in one pass.
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

# --state values that mean "every state" (no state filter at all).
NATIONAL_STATE_VALUES = {"ALL", "US", "USA", "NATIONAL"}


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

    ``state_usps=None`` means national: no state filter is applied and every
    hospital record is kept regardless of state.

    Returns ``(records, read, hospitals_matched, active_matched)``.

    We re-check the state and hospital category client-side even when the
    data-api already filtered, so the local-file path (which is unfiltered)
    produces identical results and any API quirks are caught.
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
    """Ingest hospitals for one state — or all states — from the CMS POS file.

    Parameters
    ----------
    state:
        USPS abbreviation or full name (e.g. ``"KS"`` / ``"Kansas"``), or
        ``"ALL"`` (also ``"US"`` / ``"USA"`` / ``"NATIONAL"``) to ingest every
        US hospital with no state filter.
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

    national = bool(state) and state.strip().upper() in NATIONAL_STATE_VALUES
    state_obj = None if national else resolve_state(state)
    state_usps = None if national else state_obj.usps
    state_label = "ALL" if national else state_obj.usps
    started_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    log.info(
        "Starting CMS POS ingestion for %s (%s); active_only=%s",
        "all states" if national else state_obj.name,
        state_label,
        active_only,
    )

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
                raw = cms_pos.iter_data_api_records(
                    dist,
                    state_usps=state_usps,
                    hospitals_only=True,
                )
                if limit is not None:
                    raw = itertools.islice(raw, limit)
            except cms_pos.CmsUnavailableError:
                raise
        else:
            raw = cms_pos.fetch_records(
                state_usps=state_usps,
                input_file=input_file,
                limit=limit,
            )

        records, read, hospitals_matched, active_matched = _filter_and_normalize(
            raw, state_usps, active_only
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
            # ingestion_runs.state is a 2-char column; national runs store NULL.
            state=state_usps,
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
            state_label,
        )
    else:
        # Surface the failure to the caller.
        raise cms_pos.CmsUnavailableError(message or "ingestion failed")

    return IngestSummary(
        state=state_label,
        records_read=read,
        hospitals_matched=hospitals_matched,
        active_matched=active_matched,
        loaded=loaded,
        edition=edition,
        status=status,
    )
