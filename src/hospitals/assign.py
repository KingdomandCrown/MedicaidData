"""Recording a person's decision about which hospital a file belongs to.

Every other link method is an inference. The crosswalk infers from an NPI, the
name heuristic infers from a name and a state, and the filename prefix trusts a
downloader that knew the hospital before it knew the URL. When all three fail
there is nothing left but somebody looking at the file and deciding — and until
now there was no way to write that decision down.

That gap is expensive. 144 files in this database hold 158 million charge rows
attributed to nobody, and a fresh CMS crosswalk moved none of them: the files
carry no NPI CMS has ever heard of. They are not a download problem. They are
sitting on the disk, parsed, waiting for one column to be filled in.

So: ``suggest`` writes the candidate pairs to a CSV, a person fills in the
``confirm`` column, and ``apply`` records what they decided as ``manual_review``
— a method name that stays distinguishable from the inferred ones forever,
because a judgement call should never be indistinguishable from a join.

Three refusals, all of them because a wrong attribution here is invisible
afterwards — the hospital simply appears to have prices, and they are somebody
else's:

* a CCN no hospital in the POS file has;
* a source file that is already linked, unless the caller says to relink;
* a row whose ``confirm`` column is empty, which is the ordinary case.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .db import charge_sources, hospitals
from .gap import build_gap_report
from .logging_config import get_logger

log = get_logger(__name__)

#: The method recorded for a link a person made. Deliberately not one of the
#: inferred names: "how do we know this?" has a different answer here.
MANUAL = "manual_review"

SUGGESTION_COLUMNS = (
    "confirm",
    "ccn",
    "hospital",
    "state",
    "state_known",
    "charge_rows",
    "score",
    "source_file",
    "file_hospital_name",
    "note",
)

#: What counts as "yes" in the confirm column, since people type all of these.
_YES = {"y", "yes", "1", "x", "true", "t", "ok"}


def suggest_rows(engine: Engine, *, limit: int | None = None) -> list[dict]:
    """Candidate (file, hospital) pairs for a person to rule on.

    Ordered by charge rows: the reviewer's first decision should be the one
    worth the most, and a file holding five million rows deserves attention a
    file holding nine thousand does not.
    """

    report = build_gap_report(engine)
    rows = [
        {
            "confirm": "",
            "ccn": m.ccn,
            "hospital": m.hospital,
            "state": m.state or "",
            # "yes" means the file named a state and this hospital is in it.
            # "no" means the file named none, so the name is all there is --
            # and a hospital name is not unique across states.
            "state_known": "yes" if m.same_state else "no",
            "charge_rows": m.charge_count,
            "score": f"{m.score:.3f}",
            "source_file": m.source_file,
            "file_hospital_name": m.file_hospital_name or "",
            "note": "",
        }
        for m in report.probable
    ]
    return rows[:limit] if limit else rows


def write_suggestions(rows: list[dict], path: str) -> str:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SUGGESTION_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_suggestions(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass
class Decision:
    source_file: str
    ccn: str
    status: str          # applied | skipped | unknown_ccn | unknown_file | already_linked
    note: str = ""


@dataclass
class AssignSummary:
    applied: int = 0
    skipped: int = 0
    refused: int = 0
    rows: int = 0
    decisions: list[Decision] = field(default_factory=list)

    @property
    def problems(self) -> list[Decision]:
        return [d for d in self.decisions if d.status not in ("applied", "skipped")]


def apply_links(
    engine: Engine,
    rows: list[dict],
    *,
    dry_run: bool = True,
    relink: bool = False,
) -> AssignSummary:
    """Record confirmed (file -> CCN) decisions.

    ``dry_run`` is the default because this writes attribution, and a wrong
    attribution cannot be seen afterwards: the hospital just appears to have
    prices, and they belong to somebody else.
    """

    summary = AssignSummary()

    with engine.begin() as conn:
        known = {r.ccn for r in conn.execute(select(hospitals.c.ccn)) if r.ccn}
        current = {
            r.source_file: r.ccn
            for r in conn.execute(
                select(charge_sources.c.source_file, charge_sources.c.ccn)
            )
        }

        for raw in rows:
            summary.rows += 1
            source_file = (raw.get("source_file") or "").strip()
            ccn = (raw.get("ccn") or "").strip().upper()
            confirm = (raw.get("confirm") or "").strip().lower()

            if confirm not in _YES:
                summary.skipped += 1
                summary.decisions.append(
                    Decision(source_file, ccn, "skipped", "confirm column not set")
                )
                continue

            if source_file not in current:
                summary.refused += 1
                summary.decisions.append(
                    Decision(source_file, ccn, "unknown_file",
                             "no charge source with this source_file")
                )
                continue

            if ccn not in known:
                summary.refused += 1
                summary.decisions.append(
                    Decision(source_file, ccn, "unknown_ccn",
                             "no hospital in the POS file has this CCN")
                )
                continue

            existing = current[source_file]
            if existing and existing != ccn and not relink:
                summary.refused += 1
                summary.decisions.append(
                    Decision(source_file, ccn, "already_linked",
                             f"already attributed to {existing}; pass relink to change it")
                )
                continue

            summary.applied += 1
            summary.decisions.append(Decision(source_file, ccn, "applied"))
            if not dry_run:
                conn.execute(
                    charge_sources.update()
                    .where(charge_sources.c.source_file == source_file)
                    .values(ccn=ccn, link_method=MANUAL)
                )

    log.info(
        "Assign: %d row(s) read, %d applied, %d skipped, %d refused%s",
        summary.rows,
        summary.applied,
        summary.skipped,
        summary.refused,
        " (dry run)" if dry_run else "",
    )
    return summary
