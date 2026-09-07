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

from .chna import (
    Assessment,
    consultant_hint,
    identify_template,
    parse_assessment,
    read_document,
)
from .db import (
    chna_document_hospitals,
    chna_documents,
    chna_needs,
    chna_shortages,
    hospitals,
)
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
class Survey:
    """What a folder of assessments is made of, before loading any of it.

    The first question about a new state is not "what do these say" but "who
    wrote them" — because one consultant covering forty documents is a parser
    worth writing and forty consultants covering one each is not. Answering
    that before building anything is the difference between a week per state
    and a week per document.
    """

    files: int = 0
    by_template: dict[str, int] = field(default_factory=dict)
    by_hint: dict[str, int] = field(default_factory=dict)
    unidentified: list[str] = field(default_factory=list)
    scanned: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def recognised(self) -> int:
        return sum(self.by_template.values())


def survey_path(path: str) -> Survey:
    """Read a folder and report which house formats it contains."""

    files = (
        sorted(
            os.path.join(path, name)
            for name in os.listdir(path)
            if name.lower().endswith(_EXTENSIONS) and not name.startswith(".")
        )
        if os.path.isdir(path)
        else [path]
    )

    survey = Survey()
    for file_path in files:
        survey.files += 1
        name = os.path.basename(file_path)
        try:
            text = read_document(file_path)
        except ValueError as exc:
            (survey.scanned if "OCR" in str(exc) else survey.unreadable).append(name)
            continue
        except Exception:
            survey.unreadable.append(name)
            continue

        template = identify_template(text)
        if template:
            survey.by_template[template] = survey.by_template.get(template, 0) + 1
            continue
        survey.unidentified.append(name)
        hint = consultant_hint(text)
        if hint:
            survey.by_hint[hint] = survey.by_hint.get(hint, 0) + 1

    log.info(
        "CHNA survey: %d file(s), %d in a recognised format, %d unidentified "
        "(%d naming a producer), %d needing OCR, %d unreadable",
        survey.files, survey.recognised, len(survey.unidentified),
        sum(survey.by_hint.values()), len(survey.scanned), len(survey.unreadable),
    )
    return survey


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
                    delete(chna_document_hospitals).where(
                        chna_document_hospitals.c.document_id == existing
                    )
                )
                conn.execute(
                    delete(chna_documents).where(chna_documents.c.id == existing)
                )

        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
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
                consultant_hint=header.consultant_hint,
                townhall_date=header.townhall_date,
                attendees=header.attendees,
                total_votes=header.total_votes,
                boilerplate_declines=assessment.declines_boilerplate,
                link_method=NAME_STATE if ccn else None,
                ingested_at=now,
            )
        ).inserted_primary_key[0]

        # The hospital it is filed under is also the first hospital it covers.
        # Others are added by hand, or by a system roster, and this is where
        # they go.
        if ccn:
            conn.execute(
                insert(chna_document_hospitals).values(
                    document_id=document_id, ccn=ccn,
                    link_method=NAME_STATE, linked_at=now,
                )
            )

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


def cover_hospitals(
    engine: Engine,
    source_file: str,
    ccns: list[str],
    *,
    link_method: str = MANUAL,
) -> int:
    """Record that one document also speaks for these hospitals.

    A system assessment covers facilities it never names in its title, and
    without this they stay in the gap report forever — the document is on the
    disk and the hospital still looks unassessed.

    A CCN no hospital in the POS roster has is refused rather than stored: an
    unjoinable row here is invisible, because the hospital simply continues to
    look uncovered and nothing says why.
    """

    with engine.begin() as conn:
        document_id = conn.execute(
            select(chna_documents.c.id).where(
                chna_documents.c.source_file == source_file
            )
        ).scalar()
        if document_id is None:
            raise LookupError(f"no CHNA document loaded from {source_file!r}")

        known = {r.ccn for r in conn.execute(select(hospitals.c.ccn)) if r.ccn}
        already = {
            r.ccn
            for r in conn.execute(
                select(chna_document_hospitals.c.ccn).where(
                    chna_document_hospitals.c.document_id == document_id
                )
            )
        }

        rows, refused = [], []
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        for raw in ccns:
            ccn = str(raw or "").strip().upper()
            if not ccn or ccn in already:
                continue
            if ccn not in known:
                refused.append(ccn)
                continue
            already.add(ccn)
            rows.append({
                "document_id": document_id, "ccn": ccn,
                "link_method": link_method, "linked_at": now,
            })
        if rows:
            conn.execute(insert(chna_document_hospitals), rows)

    if refused:
        log.warning(
            "CHNA: %s — %d CCN(s) not in the POS roster, not recorded: %s",
            source_file, len(refused), ", ".join(sorted(refused)[:10]),
        )
    log.info("CHNA: %s now covers %d additional hospital(s)", source_file, len(rows))
    return len(rows)


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
