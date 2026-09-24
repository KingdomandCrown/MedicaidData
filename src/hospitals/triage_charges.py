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

from . import price_transparency as pt
from .db import init_db, loaded_source_files, make_engine
from .ingest_charges import SUPPORTED, ChargeIngestSummary, ingest_charge_file
from .logging_config import get_logger

log = get_logger(__name__)

REVIEW_NOTES_FILE = "_review_notes.txt"
_OUTPUT_FOLDER_NAMES = {"_ingested", "_needs_review"}


def _default_output_root(source_dir: str) -> str:
    """Where done/review folders default to.

    Re-triaging a review folder directly (a natural thing to do after fixing
    a parser) must not nest a fresh _ingested/_needs_review inside the one
    it's already standing in — it should fold back into the same two
    folders the original run made, as siblings of source_dir's parent.
    """

    if os.path.basename(os.path.normpath(source_dir)) in _OUTPUT_FOLDER_NAMES:
        return os.path.dirname(os.path.normpath(source_dir))
    return source_dir


@dataclass
class TriageSummary:
    loaded: list[ChargeIngestSummary] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (filename, reason)
    skipped: int = 0


def triage_charges(
    source_dir: str,
    *,
    database_url: str,
    done_dir: str | None = None,
    review_dir: str | None = None,
    limit: int | None = None,
    echo_sql: bool = False,
    skip_existing: bool = False,
) -> TriageSummary:
    if not os.path.isdir(source_dir):
        raise NotADirectoryError(source_dir)

    output_root = _default_output_root(source_dir)
    done_dir = done_dir or os.path.join(output_root, "_ingested")
    review_dir = review_dir or os.path.join(output_root, "_needs_review")
    os.makedirs(done_dir, exist_ok=True)
    os.makedirs(review_dir, exist_ok=True)

    # A real drop folder arrives as one subdirectory per download round, so
    # this has to descend — but never into a "_"-prefixed folder (our own
    # done/review output, or the user's own pre-existing "_to_delete"
    # convention) or a dotfile directory, which are already-handled, not
    # source material. Relative paths are kept (not flattened to basename)
    # so two rounds that happen to share a filename can't collide on the move.
    entries: list[str] = []
    for root, dirs, names in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not d.startswith(("_", "."))]
        for name in sorted(names):
            if name == REVIEW_NOTES_FILE:
                continue  # our own bookkeeping, not a candidate file
            full = os.path.join(root, name)
            entries.append(os.path.relpath(full, source_dir))
    entries.sort()

    engine = make_engine(database_url, echo=echo_sql)
    init_db(engine)

    # A re-encountered filename isn't rejected by load_charges' own uniqueness
    # — it replaces the prior load (delete then re-insert), which on a vault
    # this size can cost minutes per file for what's usually the exact same
    # data as before. A batch this large is almost always a rerun over a
    # folder that overlaps earlier rounds, so skip what's already there
    # instead of paying that replace cost file after file.
    done: set[str] = loaded_source_files(engine) if skip_existing else set()

    summary = TriageSummary()
    total = len(entries)
    notes_path = os.path.join(review_dir, REVIEW_NOTES_FILE)

    def _reject(rel: str, reason: str) -> None:
        summary.failed.append((rel, reason))
        dest = os.path.join(review_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.join(source_dir, rel), dest)
        with open(notes_path, "a") as notes:
            notes.write(f"{rel}: {reason}\n")

    for n, rel in enumerate(entries, start=1):
        if not rel.lower().endswith(SUPPORTED):
            log.warning("[%d/%d] not an ingestible file type: %s", n, total, rel)
            _reject(rel, f"not a recognized price-transparency file type ({SUPPORTED})")
            continue
        if skip_existing and pt._strip_hash_prefix(rel) in done:
            summary.skipped += 1
            dest = os.path.join(done_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(os.path.join(source_dir, rel), dest)
            log.info("[%d/%d] skip (already loaded): %s", n, total, rel)
            continue
        log.info("[%d/%d] %s", n, total, rel)
        path = os.path.join(source_dir, rel)
        try:
            result = ingest_charge_file(
                path, database_url=database_url, limit=limit, echo_sql=echo_sql, engine=engine,
            )
            summary.loaded.append(result)
            dest = os.path.join(done_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(path, dest)
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the batch
            log.error("[%d/%d] FAILED %s: %s", n, total, rel, exc)
            _reject(rel, str(exc))

    log.info(
        "Triaged %d file(s): %d loaded into the vault, %d skipped (already loaded), "
        "%d moved to review.",
        total, len(summary.loaded), summary.skipped, len(summary.failed),
    )
    return summary
