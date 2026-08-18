"""Find charge files that are the same dataset stored more than once.

Two habits in the wild put the same rows in the database repeatedly, and they
are **not** equally fixable:

* A folder holds ``file.csv`` and ``file (1).csv`` — the same download twice.
  The second copy carries no fact the first does not. Deleting it loses
  nothing, so this module can do it.
* A large system publishes **one file per EIN** and ships a copy named for
  each facility. HCA does this: thirteen HealthOne facilities share EIN
  841321373. Here each copy's ``charge_sources`` row is the only record that
  the facility exists and what it is called, so deleting it *does* lose
  something. Fixing that is a schema change — one stored dataset, many
  facilities pointing at it — and this module only measures it.

Identical row counts under one EIN is strong evidence of shared data: two
files agreeing to the digit across three million rows are the same dataset,
not a coincidence. Which of the two habits produced them is decided by the
**filenames**, not by the hospital name recorded inside the file — a system
that publishes per facility usually stamps its own name in every copy's
metadata, so the internal name says "one hospital" when the filenames plainly
name four.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .db import charge_sources, standard_charges
from .logging_config import get_logger

log = get_logger(__name__)

# Browsers disambiguate a repeat download by appending a parenthesised
# counter: ``report (1).csv``. Nothing else about the name changes, which is
# what makes the copy safe to drop.
#
# Only this form counts. A bare ``-2`` or ``_2024`` tail is *not* treated as a
# counter, because a file can legitimately end in a number — a year, a campus
# number — and rows get deleted on the strength of this match. Missing a
# duplicate costs disk; inventing one costs data.
_COPY_SUFFIX_RE = re.compile(r"[ _]?\((\d+)\)$")


def canonical_name(source_file: str) -> str:
    """The filename with its download counter and extension removed.

    ``anmed (2).zip`` and ``anmed.zip`` share a canonical name, so they are the
    same download twice. ``wesleylonghospital...csv`` and
    ``anniepennhospital...csv`` do not, so they are two facilities publishing
    one dataset — a different problem with a different fix.
    """

    stem, _ext = os.path.splitext(os.path.basename(source_file))
    # A ``.csv.gz`` leaves ``.csv`` behind; strip one more known wrapper.
    stem, _ext2 = os.path.splitext(stem) if stem.lower().endswith(
        (".csv", ".json", ".xlsx", ".xlsm", ".zip")
    ) else (stem, "")
    return _COPY_SUFFIX_RE.sub("", stem.strip()).strip().lower()


def _keeper(names: list[str]) -> str:
    """Which copy to keep: the one without a counter, then the shortest.

    Deterministic on purpose — running the report twice must nominate the same
    survivor, or a dry run tells you nothing about what ``--apply`` will do.
    """

    def rank(name: str) -> tuple[int, int, str]:
        stem, _ = os.path.splitext(os.path.basename(name))
        has_counter = 1 if _COPY_SUFFIX_RE.search(stem.strip()) else 0
        return (has_counter, len(name), name)

    return min(names, key=rank)


@dataclass
class DuplicateGroup:
    """Files that appear to hold the same rows."""

    ein: str | None
    charge_count: int
    source_files: list[str] = field(default_factory=list)
    hospital_names: list[str] = field(default_factory=list)
    source_ids: list[int] = field(default_factory=list)

    @property
    def copies(self) -> int:
        return len(self.source_files)

    @property
    def redundant_rows(self) -> int:
        """Rows that would be freed by keeping one copy."""

        return (self.copies - 1) * self.charge_count

    @property
    def distinct_names(self) -> int:
        return len({n for n in self.hospital_names if n})

    # --- the two habits, told apart by filename ---------------------------

    @property
    def datasets(self) -> dict[str, list[str]]:
        """Source files grouped by canonical name — one entry per distinct name."""

        out: dict[str, list[str]] = {}
        for name in self.source_files:
            out.setdefault(canonical_name(name), []).append(name)
        return out

    @property
    def redownload_files(self) -> list[str]:
        """Extra copies of a byte-identical download. Safe to delete."""

        extra: list[str] = []
        for names in self.datasets.values():
            if len(names) > 1:
                keep = _keeper(names)
                extra.extend(n for n in names if n != keep)
        return sorted(extra)

    @property
    def redownload_rows(self) -> int:
        return len(self.redownload_files) * self.charge_count

    @property
    def per_facility_datasets(self) -> int:
        """Distinct filenames sharing this dataset — facilities, not downloads."""

        return len(self.datasets)

    @property
    def per_facility_rows(self) -> int:
        """Rows a schema change would save. Not safe to simply delete."""

        return (self.per_facility_datasets - 1) * self.charge_count

    @property
    def looks_like_one_file_per_facility(self) -> bool:
        """Several facilities sharing one dataset, rather than a re-download.

        Decided by the filenames. The hospital name recorded in the file is
        unreliable here: Cone Health ships four facilities' files that all
        name the *system* internally, which would read as a single hospital
        downloaded four times.
        """

        return self.per_facility_datasets > 1


@dataclass
class DuplicateReport:
    groups: list[DuplicateGroup]
    total_sources: int
    total_rows: int

    @property
    def redundant_rows(self) -> int:
        return sum(g.redundant_rows for g in self.groups)

    @property
    def redundant_files(self) -> int:
        return sum(g.copies - 1 for g in self.groups)

    @property
    def share_of_rows(self) -> float:
        return self._share(self.redundant_rows)

    # --- split by what can actually be done about it ----------------------

    @property
    def redownload_files(self) -> list[str]:
        return [name for g in self.groups for name in g.redownload_files]

    @property
    def redownload_rows(self) -> int:
        return sum(g.redownload_rows for g in self.groups)

    @property
    def redownload_share(self) -> float:
        return self._share(self.redownload_rows)

    @property
    def per_facility_rows(self) -> int:
        return sum(g.per_facility_rows for g in self.groups)

    @property
    def per_facility_share(self) -> float:
        return self._share(self.per_facility_rows)

    def _share(self, rows: int) -> float:
        return round(100.0 * rows / self.total_rows, 1) if self.total_rows else 0.0


def find_duplicate_loads(engine: Engine, *, min_rows: int = 1) -> DuplicateReport:
    """Group charge sources that share an EIN and an exact row count."""

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                charge_sources.c.id,
                charge_sources.c.ein,
                charge_sources.c.charge_count,
                charge_sources.c.source_file,
                charge_sources.c.hospital_name,
            ).where(charge_sources.c.charge_count >= max(1, min_rows))
        ).mappings().all()

        total_rows = int(
            conn.execute(select(func.coalesce(func.sum(charge_sources.c.charge_count), 0)))
            .scalar_one()
        )
        total_sources = int(
            conn.execute(select(func.count()).select_from(charge_sources)).scalar_one()
        )

    buckets: dict[tuple[str | None, int], DuplicateGroup] = {}
    for row in rows:
        # A null EIN cannot be grouped safely: two unrelated hospitals could
        # share a row count by chance with nothing else tying them together.
        if not row["ein"]:
            continue
        key = (row["ein"], row["charge_count"])
        group = buckets.setdefault(
            key, DuplicateGroup(ein=row["ein"], charge_count=row["charge_count"])
        )
        group.source_files.append(row["source_file"])
        group.hospital_names.append(row["hospital_name"])
        group.source_ids.append(row["id"])

    groups = [g for g in buckets.values() if g.copies > 1]
    groups.sort(key=lambda g: -g.redundant_rows)

    report = DuplicateReport(
        groups=groups, total_sources=total_sources, total_rows=total_rows
    )
    log.info(
        "%d duplicate group(s): %d redundant file(s) holding %d rows (%.1f%% of the store); "
        "%d row(s) are re-downloads and can be deleted, %d need a schema change",
        len(groups),
        report.redundant_files,
        report.redundant_rows,
        report.share_of_rows,
        report.redownload_rows,
        report.per_facility_rows,
    )
    return report


# --- deleting the copies that carry nothing -------------------------------


@dataclass
class PruneSummary:
    """What a prune did, or would do."""

    files: list[str]
    rows: int
    applied: bool

    @property
    def file_count(self) -> int:
        return len(self.files)


def prune_redownloads(
    engine: Engine, *, apply: bool = False, batch_size: int = 25
) -> PruneSummary:
    """Delete the extra copies of files that were simply downloaded twice.

    Only files whose canonical name matches a copy being kept are touched, so
    no facility loses its record. Dry run unless ``apply`` is true.

    A note on disk: SQLite frees the pages but does not shrink the file. The
    space is reused by the next load rather than returned to the volume —
    which is what matters here, since ``VACUUM`` would need as much free space
    again as the database is large.
    """

    report = find_duplicate_loads(engine)

    doomed: list[tuple[int, str]] = []
    rows = 0
    for group in report.groups:
        extras = set(group.redownload_files)
        if not extras:
            continue
        for source_id, name in zip(group.source_ids, group.source_files):
            if name in extras:
                doomed.append((source_id, name))
                rows += group.charge_count

    summary = PruneSummary(
        files=sorted(name for _id, name in doomed), rows=rows, applied=apply
    )

    if not apply:
        log.info(
            "Dry run: %d re-downloaded file(s) holding %d row(s) would be deleted",
            summary.file_count,
            summary.rows,
        )
        return summary

    ids = [source_id for source_id, _name in doomed]
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        # One transaction per batch: a 9-million-row delete held open across
        # the whole list would block writers for the duration and lose
        # everything on an interrupt.
        with engine.begin() as conn:
            conn.execute(
                standard_charges.delete().where(
                    standard_charges.c.source_id.in_(batch)
                )
            )
            conn.execute(
                charge_sources.delete().where(charge_sources.c.id.in_(batch))
            )
        log.info("Deleted %d of %d redundant source(s)", min(start + batch_size, len(ids)), len(ids))

    log.info(
        "Deleted %d re-downloaded file(s) holding %d row(s)",
        summary.file_count,
        summary.rows,
    )
    return summary
