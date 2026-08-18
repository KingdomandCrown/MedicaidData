"""Find charge files that are the same dataset stored more than once.

Two habits in the wild put the same rows in the database repeatedly:

* A folder holds ``file.csv`` and ``file (1).csv`` — the same download twice.
* A large system publishes **one file per EIN** and ships a copy named for
  each facility. HCA does this: thirteen HealthOne facilities share EIN
  841321373, and their row counts repeat in clusters because several names
  point at one dataset.

Identical row counts under one EIN is strong evidence: two files agreeing to
the digit across three million rows are the same data, not a coincidence.

This module **reports only**. Deleting the extra copies is not obviously
right, because a ``charge_sources`` row is also the record that a facility
exists and what it is called — dropping it loses the name even though the
charges are redundant. Storing one copy and pointing many facilities at it is
a schema change, and a deliberate one. Until then, this quantifies the cost so
the decision can be made with a number rather than a hunch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .db import charge_sources
from .logging_config import get_logger

log = get_logger(__name__)


@dataclass
class DuplicateGroup:
    """Files that appear to hold the same rows."""

    ein: str | None
    charge_count: int
    source_files: list[str] = field(default_factory=list)
    hospital_names: list[str] = field(default_factory=list)

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

    @property
    def looks_like_one_file_per_facility(self) -> bool:
        """Several facility names sharing one dataset, rather than a re-download."""

        return self.distinct_names > 1


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
        return round(100.0 * self.redundant_rows / self.total_rows, 1) if self.total_rows else 0.0


def find_duplicate_loads(engine: Engine, *, min_rows: int = 1) -> DuplicateReport:
    """Group charge sources that share an EIN and an exact row count."""

    with engine.connect() as conn:
        rows = conn.execute(
            select(
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

    groups = [g for g in buckets.values() if g.copies > 1]
    groups.sort(key=lambda g: -g.redundant_rows)

    report = DuplicateReport(
        groups=groups, total_sources=total_sources, total_rows=total_rows
    )
    log.info(
        "%d duplicate group(s): %d redundant file(s) holding %d rows (%.1f%% of the store)",
        len(groups),
        report.redundant_files,
        report.redundant_rows,
        report.share_of_rows,
    )
    return report
