"""What the knowledge base holds for a particular hospital or system.

The aggregate numbers — 250 of 7,031 covered, 632 million rows unattributed —
say how much is missing but not *why a given hospital* is. Spot-checking one
name is the question that actually gets asked: "we have 335 HCA files, so why
does HCA Florida JFK show nothing?"

Three different answers wear the same clothes, and only a per-hospital view
tells them apart:

* **No file.** Nobody downloaded it. The fix is the crawler.
* **A file, unlinked.** It is in the database with no CCN, so nothing can find
  it. The fix is the crosswalk.
* **A file linked to a sibling.** One dataset published per facility, attached
  to whichever facility the linker resolved. The fix is a schema change, and
  until then the hospital genuinely has no numbers of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.engine import Engine

from .db import charge_sources, hospitals
from .logging_config import get_logger

log = get_logger(__name__)


@dataclass
class SourceRow:
    source_file: str
    hospital_name: str | None
    ein: str | None
    primary_npi: str | None
    ccn: str | None
    link_method: str | None
    charge_count: int

    @property
    def is_linked(self) -> bool:
        return bool(self.ccn)


@dataclass
class HospitalRow:
    ccn: str
    name: str
    state: str | None
    source_count: int
    charge_rows: int

    @property
    def is_covered(self) -> bool:
        return self.source_count > 0


@dataclass
class CoverageReport:
    pattern: str
    sources: list[SourceRow] = field(default_factory=list)
    hospitals: list[HospitalRow] = field(default_factory=list)

    @property
    def linked_sources(self) -> list[SourceRow]:
        return [s for s in self.sources if s.is_linked]

    @property
    def unlinked_sources(self) -> list[SourceRow]:
        return [s for s in self.sources if not s.is_linked]

    @property
    def covered_hospitals(self) -> list[HospitalRow]:
        return [h for h in self.hospitals if h.is_covered]

    @property
    def uncovered_hospitals(self) -> list[HospitalRow]:
        return [h for h in self.hospitals if not h.is_covered]

    @property
    def charge_rows(self) -> int:
        return sum(s.charge_count or 0 for s in self.sources)

    @property
    def unlinked_rows(self) -> int:
        return sum(s.charge_count or 0 for s in self.unlinked_sources)

    @property
    def diagnosis(self) -> str:
        """One sentence naming which of the three situations this is."""

        if not self.sources and not self.hospitals:
            return "nothing matches that name in either table"
        if not self.sources:
            return (
                f"{len(self.hospitals)} hospital(s) match and no charge file does — "
                "nobody has downloaded one yet"
            )
        if not self.linked_sources:
            return (
                f"{len(self.sources)} charge file(s) are held, none linked to a CCN — "
                "the files exist but nothing can find them"
            )
        if self.uncovered_hospitals:
            return (
                f"{len(self.covered_hospitals)} of {len(self.hospitals)} matching "
                f"hospital(s) have a file; the rest share a dataset published under "
                "a sibling's name or have none"
            )
        return f"all {len(self.hospitals)} matching hospital(s) have a file"


def _boundary_re(pattern: str) -> re.Pattern:
    """Match ``pattern`` only where a word starts.

    A plain substring search for "hca" also matches heal**thca**re, so asking
    about HCA returned Adventist HealthCare, Barrett Hospital & Healthcare and
    172 others — a list with no information in it. Word starts are what a
    person means: separators here include the hyphens and underscores that
    hold a filename together, not just spaces.
    """

    return re.compile(r"(?<![a-z0-9])" + re.escape(pattern.lower().strip()))


def _matches(needle: re.Pattern, *fields: str | None) -> bool:
    return any(needle.search((f or "").lower()) for f in fields)


def coverage_for(
    engine: Engine, pattern: str, *, state: str | None = None, limit: int = 200
) -> CoverageReport:
    """Everything the database knows about hospitals and files matching ``pattern``.

    Matched case-insensitively against the hospital name, the name recorded in
    the charge file, and the filename — a system-published file often names the
    system in one and the facility in another. The database narrows with a
    substring search; the word-boundary test is applied to what comes back,
    because SQL LIKE cannot express "where a word starts".
    """

    needle = _boundary_re(pattern)
    like = f"%{pattern.lower()}%"
    report = CoverageReport(pattern=pattern)

    with engine.connect() as conn:
        source_stmt = select(
            charge_sources.c.source_file,
            charge_sources.c.hospital_name,
            charge_sources.c.ein,
            charge_sources.c.primary_npi,
            charge_sources.c.ccn,
            charge_sources.c.link_method,
            charge_sources.c.charge_count,
        ).where(
            or_(
                func.lower(charge_sources.c.source_file).like(like),
                func.lower(charge_sources.c.hospital_name).like(like),
            )
        )
        for row in conn.execute(source_stmt).mappings():
            if not _matches(needle, row["source_file"], row["hospital_name"]):
                continue
            report.sources.append(
                SourceRow(
                    source_file=row["source_file"],
                    hospital_name=row["hospital_name"],
                    ein=row["ein"],
                    primary_npi=row["primary_npi"],
                    ccn=row["ccn"],
                    link_method=row["link_method"],
                    charge_count=row["charge_count"] or 0,
                )
            )

        # Left join so a hospital with no file still appears — its absence is
        # the whole point of the question.
        counted = (
            select(
                charge_sources.c.ccn.label("ccn"),
                func.count().label("source_count"),
                func.coalesce(func.sum(charge_sources.c.charge_count), 0).label("rows"),
            )
            .where(charge_sources.c.ccn.is_not(None))
            .group_by(charge_sources.c.ccn)
            .subquery()
        )

        hosp_stmt = (
            select(
                hospitals.c.ccn,
                hospitals.c.name,
                hospitals.c.state,
                func.coalesce(counted.c.source_count, 0).label("source_count"),
                func.coalesce(counted.c.rows, 0).label("rows"),
            )
            .select_from(hospitals.outerjoin(counted, hospitals.c.ccn == counted.c.ccn))
            .where(func.lower(hospitals.c.name).like(like))
        )
        if state:
            hosp_stmt = hosp_stmt.where(hospitals.c.state == state.upper())

        for row in conn.execute(hosp_stmt).mappings():
            if not _matches(needle, row["name"]):
                continue
            report.hospitals.append(
                HospitalRow(
                    ccn=row["ccn"],
                    name=row["name"],
                    state=row["state"],
                    source_count=int(row["source_count"]),
                    charge_rows=int(row["rows"]),
                )
            )

    report.sources.sort(key=lambda s: -s.charge_count)
    report.hospitals.sort(key=lambda h: (h.is_covered, h.name))
    del report.sources[limit:]
    del report.hospitals[limit:]

    log.info(
        "Coverage for %r: %d file(s) (%d linked), %d matching hospital(s) (%d covered)",
        pattern,
        len(report.sources),
        len(report.linked_sources),
        len(report.hospitals),
        len(report.covered_hospitals),
    )
    return report
