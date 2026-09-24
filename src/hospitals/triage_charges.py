"""Ingest a drop folder of price-transparency files and file each one by outcome.

A one-off download folder (e.g. a Desktop drop of hospital MRFs pulled from
many different sites) needs the same per-file ingest as ``ingest-charges``,
but it also needs to end up empty: every file that loaded moves out to a
"done" folder, and every file that didn't — wrong format, no recognizable
header, not even an MRF — moves to a "review" folder alongside a plain-text
note of why. The source folder itself becomes the answer to "what's left to
deal with" instead of a log scroll, and is safe to delete once emptied.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .db import init_db, make_engine
from .ingest_charges import SUPPORTED, ChargeIngestSummary, ingest_charge_file
from .logging_config import get_logger

log = get_logger(__name__)

REVIEW_NOTES_FILE = "_review_notes.txt"


@dataclass
class TriageSummary:
    loaded: list[ChargeIngestSummary] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (filename, reason)


def triage_charges(
    source_dir: str,
    *,
    database_url: str,
    done_dir: str | None = None,
    review_dir: str | None = None,
    limit: int | None = None,
    echo_sql: bool = False,
) -> TriageSummary:
    if not os.path.isdir(source_dir):
        raise NotADirectoryError(source_dir)

    done_dir = done_dir or os.path.join(source_dir, "_ingested")
    review_dir = review_dir or os.path.join(source_dir, "_needs_review")
    os.makedirs(done_dir, exist_ok=True)
    os.makedirs(review_dir, exist_ok=True)

    # Sorted up front, and as plain filenames: once a file moves, a path built
    # from the original directory listing would no longer resolve.
    entries = sorted(
        name
        for name in os.listdir(source_dir)
        if os.path.isfile(os.path.join(source_dir, name))
    )

    engine = make_engine(database_url, echo=echo_sql)
    init_db(engine)

    summary = TriageSummary()
    total = len(entries)
    notes_path = os.path.join(review_dir, REVIEW_NOTES_FILE)

    def _reject(name: str, reason: str) -> None:
        summary.failed.append((name, reason))
        shutil.move(os.path.join(source_dir, name), os.path.join(review_dir, name))
        with open(notes_path, "a") as notes:
            notes.write(f"{name}: {reason}\n")

    for n, name in enumerate(entries, start=1):
        if not name.lower().endswith(SUPPORTED):
            log.warning("[%d/%d] not an ingestible file type: %s", n, total, name)
            _reject(name, f"not a recognized price-transparency file type ({SUPPORTED})")
            continue
        log.info("[%d/%d] %s", n, total, name)
        path = os.path.join(source_dir, name)
        try:
            result = ingest_charge_file(
                path, database_url=database_url, limit=limit, echo_sql=echo_sql, engine=engine,
            )
            summary.loaded.append(result)
            shutil.move(path, os.path.join(done_dir, name))
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the batch
            log.error("[%d/%d] FAILED %s: %s", n, total, name, exc)
            _reject(name, str(exc))

    log.info(
        "Triaged %d file(s): %d loaded into the vault, %d moved to review.",
        total, len(summary.loaded), len(summary.failed),
    )
    return summary
