"""Move ingested price-transparency files out of the working folder.

A download folder of several hundred MRFs is hard to reason about once most of
them are loaded: the files still to deal with are buried among the ones that
are done. Clearing the finished ones out leaves the folder as the worklist.

What counts as "done" is decided by the database, not the filename — a file is
moved only when ``charge_sources`` says it loaded at least one charge row. So a
file that failed to parse, or parsed to nothing, stays exactly where it is.

Nothing is deleted and nothing is overwritten: files are *moved* to a folder you
name, and a name already taken at the destination is reported rather than
clobbered. The move is a dry run unless you ask for it.
"""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass, field

from . import price_transparency as pt
from .db import loaded_source_files, make_engine
# Whatever the ingester recognizes; anything else in the folder is left alone
# rather than guessed about. Imported rather than repeated so the two lists
# cannot drift apart and strand a file type the ingester can actually read.
from .ingest_charges import SUPPORTED
from .logging_config import get_logger

log = get_logger(__name__)


@dataclass
class ArchiveSummary:
    moved: list[str] = field(default_factory=list)
    not_loaded: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total_candidates(self) -> int:
        return len(self.moved) + len(self.not_loaded) + len(self.collisions)


def _candidates(path: str, recursive: bool) -> tuple[list[str], list[str]]:
    """Files to consider, and the subdirectories passed over."""

    if not recursive:
        entries = sorted(glob.glob(os.path.join(path, "*")))
        files = [e for e in entries if not os.path.isdir(e)]
        skipped = [os.path.basename(e) for e in entries if os.path.isdir(e)]
        return files, skipped

    files = []
    for root, dirs, names in os.walk(path):
        dirs.sort()
        files.extend(os.path.join(root, n) for n in sorted(names))
    return files, []


def archive_ingested(
    path: str,
    destination: str,
    *,
    database_url: str = "sqlite:///data/hospitals.sqlite",
    apply: bool = False,
    recursive: bool = False,
) -> ArchiveSummary:
    """Move files from ``path`` into ``destination`` once they are loaded.

    Returns a summary of what moved and what was left behind. With
    ``apply=False`` (the default) nothing is touched — the summary describes
    what *would* happen.

    ``recursive`` descends into subdirectories, mirroring each file's relative
    path under ``destination``. The ingester grew ``--recursive`` when a system
    arrived as a folder of 335 files; this did not, so that folder could be
    loaded and then never cleared. Without it, subdirectories are named in the
    summary rather than passed over in silence — the silence is what made a
    folder full of already-loaded files look like work still to do.
    """

    if not os.path.isdir(path):
        raise NotADirectoryError(path)

    engine = make_engine(database_url)
    loaded = loaded_source_files(engine)
    if not loaded:
        log.warning(
            "No loaded files recorded in %s — nothing will be moved", database_url
        )

    summary = ArchiveSummary(dry_run=not apply)
    entries, summary.skipped_dirs = _candidates(path, recursive)

    for entry in entries:
        # Relative to the folder being tidied, so a subdirectory is recreated
        # at the destination rather than flattened into it — two systems can
        # each publish a "standardcharges.json" without colliding.
        rel = os.path.relpath(entry, path)
        name = os.path.basename(entry)
        if not name.lower().endswith(SUPPORTED):
            summary.other_files.append(rel)
            continue

        # Match on the same key the ingester stored.
        key = pt._strip_hash_prefix(entry)
        if key not in loaded:
            summary.not_loaded.append(rel)
            continue

        target = os.path.join(destination, rel)
        if os.path.exists(target):
            summary.collisions.append(rel)
            log.warning("Already present at destination, leaving in place: %s", rel)
            continue

        summary.moved.append(rel)
        if apply:
            os.makedirs(os.path.dirname(target) or destination, exist_ok=True)
            shutil.move(entry, target)
            log.info("Moved %s", rel)

    log.info(
        "%s: %d of %d ingestible file(s) %s to %s (%d not loaded, %d name clashes)",
        path,
        len(summary.moved),
        summary.total_candidates,
        "would move" if summary.dry_run else "moved",
        destination,
        len(summary.not_loaded),
        len(summary.collisions),
    )
    if summary.skipped_dirs:
        log.warning(
            "Did not descend into %d subdirector%s: %s — pass recursive=True to include them",
            len(summary.skipped_dirs),
            "y" if len(summary.skipped_dirs) == 1 else "ies",
            ", ".join(summary.skipped_dirs[:5]),
        )
    return summary
