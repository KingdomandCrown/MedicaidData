"""Correct identifiers already written to the database.

A parser fix only helps the next load. Rows written while the parser was wrong
keep the wrong value until something goes back and rewrites them, and a wrong
EIN is not a cosmetic problem: it is the key that ties a hospital's charge rows
to its peers, so a file misfiled under an invented organization is invisible to
every benchmark that hospital appears in.

The specific damage this repairs: a ten-digit organizational NPI at the front
of a filename was read as a nine-digit EIN plus a spare digit, so Atrium's NPI
``1669348991`` became "EIN" ``166934899`` and 8.5 million charge rows were
filed under an employer identification number that does not exist.

Deriving the truth from the filename is safe to repeat — it reads the same
name and computes the same answer — so this can be run again after any future
change to the filename parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .db import charge_sources, standard_charges
from .logging_config import get_logger
from .price_transparency import ein_from_filename, npi_from_filename

log = get_logger(__name__)


@dataclass
class EinFix:
    """One source file whose stored EIN disagrees with its filename."""

    source_id: int
    source_file: str
    stored_ein: str | None
    correct_ein: str | None
    npi: str | None
    charge_count: int

    @property
    def drops_an_invented_ein(self) -> bool:
        """The stored EIN came from nowhere — the filename holds only an NPI."""

        return self.correct_ein is None and self.stored_ein is not None


@dataclass
class EinRepairSummary:
    fixes: list[EinFix] = field(default_factory=list)
    applied: bool = False
    charge_rows_rewritten: int = 0
    sources_only: bool = False

    @property
    def source_count(self) -> int:
        return len(self.fixes)

    @property
    def charge_rows_affected(self) -> int:
        return sum(f.charge_count or 0 for f in self.fixes)


def find_ein_mismatches(engine: Engine) -> list[EinFix]:
    """Compare every stored EIN against what its filename says today."""

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                charge_sources.c.id,
                charge_sources.c.source_file,
                charge_sources.c.ein,
                charge_sources.c.primary_npi,
                charge_sources.c.charge_count,
            )
        ).mappings().all()

    fixes: list[EinFix] = []
    for row in rows:
        correct = ein_from_filename(row["source_file"])
        if correct == row["ein"]:
            continue
        fixes.append(
            EinFix(
                source_id=row["id"],
                source_file=row["source_file"],
                stored_ein=row["ein"],
                correct_ein=correct,
                npi=row["primary_npi"] or npi_from_filename(row["source_file"]),
                charge_count=row["charge_count"] or 0,
            )
        )
    fixes.sort(key=lambda f: -f.charge_count)
    return fixes


def repair_eins(
    engine: Engine, *, apply: bool = False, sources_only: bool = False
) -> EinRepairSummary:
    """Rewrite stored EINs to match what their filenames say.

    ``sources_only`` updates the one row per file in ``charge_sources`` and
    leaves the charge rows alone — instant, and enough to make the file itself
    findable. The full repair also rewrites ``standard_charges.ein``, which
    touches every charge row of every affected file and is the expensive part;
    prune redundant copies first so the work is not done on rows about to be
    deleted.
    """

    fixes = find_ein_mismatches(engine)
    summary = EinRepairSummary(fixes=fixes, applied=apply, sources_only=sources_only)

    if not apply or not fixes:
        log.info(
            "%d source(s) carry an EIN their filename disagrees with, covering %d charge row(s)",
            summary.source_count,
            summary.charge_rows_affected,
        )
        return summary

    for fix in fixes:
        # One transaction per file: an interrupt leaves earlier files repaired
        # and this one untouched, never a source and its charges disagreeing.
        with engine.begin() as conn:
            values = {"ein": fix.correct_ein}
            if fix.npi:
                values["primary_npi"] = fix.npi
            conn.execute(
                charge_sources.update()
                .where(charge_sources.c.id == fix.source_id)
                .values(**values)
            )
            if not sources_only:
                result = conn.execute(
                    standard_charges.update()
                    .where(standard_charges.c.source_id == fix.source_id)
                    .values(ein=fix.correct_ein)
                )
                summary.charge_rows_rewritten += result.rowcount or 0
        log.info(
            "Repaired %s: EIN %s -> %s",
            fix.source_file,
            fix.stored_ein,
            fix.correct_ein,
        )

    log.info(
        "Repaired %d source(s); rewrote %d charge row(s)",
        summary.source_count,
        summary.charge_rows_rewritten,
    )
    return summary


# --- filling in an NPI the file never carried -----------------------------


@dataclass
class NpiBackfill:
    source_id: int
    source_file: str
    npi: str
    charge_count: int


@dataclass
class NpiBackfillSummary:
    fixes: list[NpiBackfill] = field(default_factory=list)
    applied: bool = False

    @property
    def source_count(self) -> int:
        return len(self.fixes)

    @property
    def charge_rows_covered(self) -> int:
        return sum(f.charge_count or 0 for f in self.fixes)


def find_missing_npis(engine: Engine) -> list[NpiBackfill]:
    """Sources with no NPI stored whose filename supplies one.

    Many published JSON files omit ``type_2_npi`` entirely — every Dignity
    Health file in round 6 logged ``NPI None`` — while naming the NPI in the
    filename. The NPI is the only key that resolves a system-level file to a
    single hospital, so a file missing it cannot be linked at all.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            select(
                charge_sources.c.id,
                charge_sources.c.source_file,
                charge_sources.c.npis,
                charge_sources.c.primary_npi,
                charge_sources.c.charge_count,
            ).where(charge_sources.c.primary_npi.is_(None))
        ).mappings().all()

    out: list[NpiBackfill] = []
    for row in rows:
        if row["npis"]:
            continue
        npi = npi_from_filename(row["source_file"])
        if not npi:
            continue
        out.append(
            NpiBackfill(
                source_id=row["id"],
                source_file=row["source_file"],
                npi=npi,
                charge_count=row["charge_count"] or 0,
            )
        )
    out.sort(key=lambda f: -f.charge_count)
    return out


def backfill_npis(engine: Engine, *, apply: bool = False) -> NpiBackfillSummary:
    """Store the filename's NPI on sources that have none.

    Only ``charge_sources`` is touched: ``standard_charges.primary_npi`` is a
    denormalized copy used for filtering, not for linking, and rewriting
    hundreds of millions of rows to fill it in is not worth doing for that.
    """

    fixes = find_missing_npis(engine)
    summary = NpiBackfillSummary(fixes=fixes, applied=apply)

    if not apply or not fixes:
        log.info(
            "%d source(s) have no NPI but a filename that supplies one, covering "
            "%d charge row(s)",
            summary.source_count,
            summary.charge_rows_covered,
        )
        return summary

    with engine.begin() as conn:
        for fix in fixes:
            conn.execute(
                charge_sources.update()
                .where(charge_sources.c.id == fix.source_id)
                .values(primary_npi=fix.npi)
            )
    log.info("Filled in the NPI for %d source(s)", summary.source_count)
    return summary
