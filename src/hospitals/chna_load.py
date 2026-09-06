"""Loading parsed assessments into the knowledge base.

Attribution works the way it does everywhere else here: a document is joined
to a hospital by name and state, and when that is ambiguous the document is
loaded *unattributed* rather than guessed at. A CHNA filed against the wrong
hospital would put another community's priorities on a scorecard, and nothing
downstream would look wrong.

Two facts about this corpus shape the loader.

**One assessment often covers several hospitals.** Mercy publishes a single
system CHNA and posts it under each facility's name — three filenames, one
document, byte for byte. Collapsing those loses two hospitals; treating them
as three assessments triple-counts the priorities. So the source file is the
unique key and the CCN is a separate column, exactly as charge sources work.

**A parsed value and a guess must not look alike.** Ranked needs come from a
table and are facts about the document. Shortages come from matching a
specialty against a cue in the same sentence, which is a good heuristic that
will sometimes read a hospital's service list as a complaint. Those load with
``confirmed = False`` and stay that way until a person says otherwise.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from .chna import Assessment, parse_assessment, read_document
from .db import chna_documents, chna_needs, chna_shortages, hospitals
from .link import normalize_name
from .logging_config import get_logger

log = get_logger(__name__)

#: How a document was attributed to a hospital.
NAME_STATE = "name_state"
MANUAL = "manual_review"

_EXTENSIONS = (".pdf", ".txt")


@dataclass
class LoadedDocument:
    source_file: str
    hospital: str | None
    ccn: str | None
    needs: int
    shortages: int
    status: str            # loaded | unattributed | ambiguous | scanned | unreadable
    note: str = ""


@dataclass
class ChnaSummary:
    files: int = 0
    loaded: int = 0
    attributed: int = 0
    scanned: int = 0
    failed: int = 0
    documents: list[LoadedDocument] = field(default_factory=list)

    @property
    def unattributed(self) -> list[LoadedDocument]:
        return [d for d in self.documents if d.status in ("unattributed", "ambiguous")]

    @property
    def problems(self) -> list[LoadedDocument]:
        return [d for d in self.documents if d.status in ("scanned", "unreadable")]


def _roster(engine: Engine) -> dict[tuple[str, str], set[str]]:
    """(normalized name, state) -> CCNs, for attributing a document."""

    index: dict[tuple[str, str], set[str]] = {}
    with engine.connect() as conn:
        for row in conn.execute(
            select(hospitals.c.ccn, hospitals.c.name, hospitals.c.state)
        ):
            if not row.ccn or not row.state:
                continue
            key = (normalize_name(row.name), row.state.upper())
            if key[0]:
                index.setdefault(key, set()).add(row.ccn)
    return index


def attribute(assessment: Assessment, roster: dict) -> tuple[str | None, str]:
    """Find the CCN this assessment belongs to.

    Returns ``(ccn, status)``. Two hospitals sharing a name in one state is
    not a match — that is a coin flip wearing a join's clothes.
    """

    name = assessment.header.hospital
    state = (assessment.header.state or "").upper()
    if not name or not state:
        return None, "unattributed"

    candidates = roster.get((normalize_name(name), state))
    if not candidates:
        return None, "unattributed"
    if len(candidates) > 1:
        return None, "ambiguous"
    return next(iter(candidates)), "loaded"


def _need_rows(document_id: int, assessment: Assessment) -> list[dict]:
    rows = []
    for kind, needs in (("priority", assessment.needs), ("ongoing", assessment.trend)):
        for need in needs:
            rows.append(
                {
                    "document_id": document_id,
                    "table_kind": kind,
                    "rank": need.rank,
                    "label": need.label,
                    "votes": need.votes,
                    "pct": need.pct,
                    "accum": need.accum,
                    "prior_rank": need.prior_rank,
                    "recovered": need.recovered,
                }
            )
    return rows


def load_assessment(
    engine: Engine,
    path: str,
    *,
    roster: dict | None = None,
    replace: bool = True,
) -> LoadedDocument:
    """Parse one file and write it. Re-loading replaces rather than duplicates."""

    source_file = os.path.basename(path)
    try:
        text = read_document(path)
    except ValueError as exc:
        status = "scanned" if "OCR" in str(exc) else "unreadable"
        return LoadedDocument(source_file, None, None, 0, 0, status, str(exc))
    except Exception as exc:  # a corrupt or encrypted PDF should not end a batch
        return LoadedDocument(source_file, None, None, 0, 0, "unreadable", str(exc))

    assessment = parse_assessment(text)
    index = _roster(engine) if roster is None else roster
    ccn, status = attribute(assessment, index)
    header = assessment.header

    with engine.begin() as conn:
        if replace:
            existing = conn.execute(
                select(chna_documents.c.id).where(
                    chna_documents.c.source_file == source_file
                )
            ).scalar()
            if existing is not None:
                # Children go first: SQLite does not enforce ON DELETE CASCADE
                # unless foreign keys are switched on, and relying on that here
                # would leave orphaned needs behind on some engines and not
                # others.
                conn.execute(
                    delete(chna_needs).where(chna_needs.c.document_id == existing)
                )
                conn.execute(
                    delete(chna_shortages).where(
                        chna_shortages.c.document_id == existing
                    )
                )
                conn.execute(
                    delete(chna_documents).where(chna_documents.c.id == existing)
                )

        document_id = conn.execute(
            insert(chna_documents).values(
                source_file=source_file,
                ccn=ccn,
                hospital_name=header.hospital,
                county=header.county,
                state=header.state,
                year=header.year,
                cycle_label=header.cycle_label,
                consultant=header.consultant,
                townhall_date=header.townhall_date,
                attendees=header.attendees,
                total_votes=header.total_votes,
                boilerplate_declines=assessment.declines_boilerplate,
                link_method=NAME_STATE if ccn else None,
                ingested_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
            )
        ).inserted_primary_key[0]

        needs = _need_rows(document_id, assessment)
        if needs:
            conn.execute(insert(chna_needs), needs)

        shortages = [
            {
                "document_id": document_id,
                "specialty": s.specialty,
                "verbatim": s.verbatim,
                "recruiting": s.recruiting,
                "confirmed": False,
            }
            for s in assessment.shortages
        ]
        if shortages:
            conn.execute(insert(chna_shortages), shortages)

    return LoadedDocument(
        source_file=source_file,
        hospital=header.hospital,
        ccn=ccn,
        needs=len(needs),
        shortages=len(shortages),
        status=status,
        note="" if ccn else "no single hospital in the POS roster matches this name",
    )


def load_path(engine: Engine, path: str, *, replace: bool = True) -> ChnaSummary:
    """Load one file or a whole folder of them."""

    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, name)
            for name in os.listdir(path)
            if name.lower().endswith(_EXTENSIONS) and not name.startswith(".")
        )
    else:
        files = [path]

    summary = ChnaSummary()
    roster = _roster(engine)

    for file_path in files:
        summary.files += 1
        result = load_assessment(engine, file_path, roster=roster, replace=replace)
        summary.documents.append(result)
        if result.status in ("scanned", "unreadable"):
            summary.failed += 1
            if result.status == "scanned":
                summary.scanned += 1
            log.warning("CHNA: %s — %s", result.source_file, result.note)
            continue
        summary.loaded += 1
        if result.ccn:
            summary.attributed += 1

    log.info(
        "CHNA: %d file(s), %d loaded, %d attributed to a hospital, "
        "%d needing OCR, %d unreadable",
        summary.files,
        summary.loaded,
        summary.attributed,
        summary.scanned,
        summary.failed - summary.scanned,
    )
    return summary
