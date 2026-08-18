"""Find price-transparency files on disk that the database already holds.

``archive-ingested`` tidies one folder you name. This answers the broader
question — *what, anywhere on this machine, can go?* — by walking several roots
at once and reporting how much disk each already-loaded file is holding.

It reads and reports. Nothing is moved or deleted, because "already ingested"
is a claim about a database and the file is the only other copy: worth a look
before acting on it. The listing it writes is the input to whatever you decide.

What counts as loaded is what ``charge_sources`` says loaded at least one
charge row — the same rule the archiver uses. A file that failed to parse, or
parsed to nothing, is reported as still needed.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

from . import price_transparency as pt
from .db import loaded_source_files, make_engine
from .ingest_charges import SUPPORTED
from .logging_config import get_logger

log = get_logger(__name__)


@dataclass
class FoundFile:
    path: str
    size: int
    key: str  # the name the database stores

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class ScanSummary:
    ingested: list[FoundFile] = field(default_factory=list)
    not_ingested: list[FoundFile] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(f.size for f in self.ingested)

    @property
    def kept_bytes(self) -> int:
        return sum(f.size for f in self.not_ingested)

    @property
    def by_directory(self) -> dict[str, tuple[int, int]]:
        """directory -> (file count, bytes) for the already-ingested files."""

        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for found in self.ingested:
            entry = out[found.directory]
            entry[0] += 1
            entry[1] += found.size
        return {d: (c, b) for d, (c, b) in out.items()}

    @property
    def duplicate_names(self) -> dict[str, list[str]]:
        """Files carrying one database key from more than one place.

        The database stores a file once, by name. Two copies in two folders
        both look ingested, and both are — but deleting both leaves no copy at
        all, which is a different decision from deleting a redundant one.
        """

        seen: dict[str, list[str]] = defaultdict(list)
        for found in self.ingested:
            seen[found.key].append(found.path)
        return {k: sorted(v) for k, v in seen.items() if len(v) > 1}


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:,.1f} TB"  # pragma: no cover - unreachable


def scan_for_ingested(
    roots: list[str],
    *,
    database_url: str = "sqlite:///data/hospitals.sqlite",
) -> ScanSummary:
    """Walk ``roots`` and classify every MRF file against the database."""

    engine = make_engine(database_url)
    loaded = loaded_source_files(engine)
    if not loaded:
        log.warning(
            "No loaded files recorded in %s — everything will look unneeded",
            database_url,
        )

    summary = ScanSummary()

    for root in roots:
        expanded = os.path.expanduser(root)
        if not os.path.isdir(expanded):
            summary.missing_roots.append(root)
            log.warning("Not a directory, skipping: %s", root)
            continue
        summary.roots.append(expanded)

        for dirpath, dirnames, filenames in os.walk(expanded):
            dirnames.sort()
            for name in sorted(filenames):
                if not name.lower().endswith(SUPPORTED):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:  # a broken link, or gone mid-walk
                    continue
                key = pt._strip_hash_prefix(path)
                found = FoundFile(path=path, size=size, key=key)
                if key in loaded:
                    summary.ingested.append(found)
                else:
                    summary.not_ingested.append(found)

    summary.ingested.sort(key=lambda f: -f.size)
    summary.not_ingested.sort(key=lambda f: -f.size)

    log.info(
        "Scanned %d root(s): %d file(s) already ingested holding %s, "
        "%d still needed holding %s",
        len(summary.roots),
        len(summary.ingested),
        human_bytes(summary.reclaimable_bytes),
        len(summary.not_ingested),
        human_bytes(summary.kept_bytes),
    )
    return summary


def write_listing(summary: ScanSummary, path: str) -> int:
    """Write the already-ingested paths, one per line, for review."""

    with open(path, "w", encoding="utf-8") as fh:
        for found in summary.ingested:
            fh.write(found.path + "\n")
    log.info("Wrote %d path(s) to %s", len(summary.ingested), path)
    return len(summary.ingested)
