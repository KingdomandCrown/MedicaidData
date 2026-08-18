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
    dry_run: bool = True

    @property
    def total_candidates(self) -> int:
        return len(self.moved) + len(self.not_loaded) + len(self.collisions)


def archive_ingested(
    path: str,
    destination: str,
    *,
    database_url: str = "sqlite:///data/hospitals.sqlite",
    apply: bool = False,
) -> ArchiveSummary:
    """Move files from ``path`` into ``destination`` once they are loaded.

    Returns a summary of what moved and what was left behind. With
    ``apply=False`` (the default) nothing is touched — the summary describes
    what *would* happen.
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

    for entry in sorted(glob.glob(os.path.join(path, "*"))):
        if os.path.isdir(entry):
            continue
        name = os.path.basename(entry)
        if not name.lower().endswith(SUPPORTED):
            summary.other_files.append(name)
            continue

        # Match on the same key the ingester stored.
        key = pt._strip_hash_prefix(entry)
        if key not in loaded:
            summary.not_loaded.append(name)
            continue

        target = os.path.join(destination, name)
        if os.path.exists(target):
            summary.collisions.append(name)
            log.warning("Already present at destination, leaving in place: %s", name)
            continue

        summary.moved.append(name)
        if apply:
            os.makedirs(destination, exist_ok=True)
            shutil.move(entry, target)
            log.info("Moved %s", name)

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
    return summary
