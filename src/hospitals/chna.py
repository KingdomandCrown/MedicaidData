"""Reading a community health needs assessment.

Every non-profit hospital must publish one every three years and adopt an
implementation strategy against it, or pay $50,000 per facility under IRC
§4959. What comes out is the only document where a hospital states publicly
what its community's problems are — and, because the Treasury regulations
require it, which of those problems it has decided not to fix.

That makes them worth reading. Sixty of them is nobody's job, which is why
nobody has.

**These are mostly one template.** A large share of Kansas assessments are
produced by VVV Consultants of Olathe: same section order, same tables, in
places the same sentences with the numbers swapped. So the majority of the
corpus is a parser rather than a language model with a review queue behind
it — deterministic, fast, and incapable of inventing a commitment a hospital
never made. The model is the fallback for the minority written free-hand.

What this reads:

* the title block — hospital, county, cycle year, wave or round, consultant
* the town hall line — date, attendees, total votes cast
* the priority tally — the ranked needs with their votes
* the ongoing-problem table, which carries a *prior rank* and is therefore
  cycle-over-cycle movement the consultant has already computed
* provider shortages, which arrive named by specialty

The one thing standing in the way is that PDF text extraction collapses
letter-spaced table cells, and it does it to some rows and not others. See
:func:`dekern`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logging_config import get_logger

log = get_logger(__name__)

#: Fingerprint of the template this parser is written against.
VVV = "vvv"


@dataclass(frozen=True)
class Template:
    """A consultant whose house format we can recognise.

    Kansas is nearly one consultant. The country is not: every state has its
    own handful, and a national build that hardcodes one of them needs a code
    change per state. So a template is *data* — a name and the strings that
    identify it — and adding one is a line in this tuple, not a new branch in
    the parser.

    ``tabular`` says whether the format carries the ranked vote tally this
    module knows how to read. A recognised template without it is still worth
    naming: it tells you which parser to write next, and how many documents
    writing it would unlock.
    """

    name: str
    markers: tuple[str, ...]
    tabular: bool = False


#: Recognised formats. Only VVV has been read closely enough to parse; the
#: rest are named so a survey of a new state's corpus reports what it is made
#: of rather than a single undifferentiated "unknown" pile. Add a template
#: here the moment its name shows up in a survey — recognising a format costs
#: nothing and tells you what the next parser is worth.
TEMPLATES: tuple[Template, ...] = (
    Template(VVV, ("vvv consultants", "vvv research", "vandehaar"), tabular=True),
)

# "prepared by X", "facilitated by X", "conducted by X in partnership with"
# -- how an unrecognised document tells you who wrote it, so the next template
# entry is a fact rather than a guess.
_PREPARED_BY = re.compile(
    r"(?:prepared|facilitated|conducted|completed|authored|produced)\s+"
    r"(?:for\s+\S+\s+)?by[:\s]+([A-Z][A-Za-z0-9 ,.&'’-]{3,60})",
    re.IGNORECASE,
)

# The IRS requires a hospital to say which identified needs it will not
# address and why. In practice that answer is this sentence, verbatim, in
# hospitals of every size across the state. Worth recognising as boilerplate
# rather than reading it as a considered position.
DECLINE_BOILERPLATE = re.compile(
    r"community\s+social\s+determinant.{0,40}?not\s+part\s+"
    r"hospital.{0,3}s?\s+mission",
    re.IGNORECASE | re.DOTALL,
)


# --- de-kerning -------------------------------------------------------------


# Four or more single characters separated by one or two spaces. Not preceded
# by a letter, so a real word followed by initials is left alone -- but a digit
# may precede, because "6H o m e  H e a l t h" is exactly how a rank column
# runs into a kerned label.
_KERNED_RUN = re.compile(r"(?<![A-Za-z])(\S(?: {1,2}\S){3,})")


def _collapse(match: re.Match) -> str:
    # Inside the run a double space is a word break and a single space is
    # nothing at all.
    return " ".join(word.replace(" ", "") for word in re.split(r" {2}", match.group(1)))


def dekern(line: str) -> str:
    """Rejoin text whose letters were extracted one space apart.

    PDF extraction turns a letter-spaced table cell into ``H o m e  H e a l
    t h``, and it does this to some rows of a table and not others — row six
    of Patterson Health Center's priority table collapses while rows one
    through five come out clean. Left alone the sixth priority silently
    disappears from every document in the corpus, and nothing about the
    output looks wrong, which is the worst way for a parser to fail.

    Inside a kerned run a single space joins letters and a double space is a
    real word break, so both survive. Text that was never kerned is returned
    unchanged.
    """

    if not line.strip():
        return line
    return _KERNED_RUN.sub(_collapse, line)


# --- splitting a run-together number blob ----------------------------------


def split_votes_and_pct(blob: str, total_votes: int | None) -> tuple[int, float] | None:
    """Recover ``(votes, percent)`` from digits that ran together.

    De-kerning ``7 6 . 5 %`` gives ``76.5%``, which is seven votes at 6.5 per
    cent, not seventy-six votes at half a per cent. Nothing in the string says
    which — but the table prints its own vote total, and only one reading is
    consistent with it. Seven of 108 is 6.5%; seventy-six of 108 is 70%.

    Returns ``None`` when no split reconciles, rather than guessing. A wrong
    vote count here would be invisible afterwards: the number is plausible and
    no other column contradicts it.
    """

    text = blob.strip().rstrip("%")
    if "." not in text:
        return None

    whole, _, frac = text.partition(".")
    for cut in range(1, len(whole)):
        votes_text, pct_text = whole[:cut], whole[cut:]
        try:
            votes = int(votes_text)
            pct = float(f"{pct_text}.{frac}")
        except ValueError:
            continue
        if total_votes:
            implied = 100.0 * votes / total_votes
            if abs(implied - pct) <= 0.6:
                return votes, pct
        elif 0 < pct <= 100:
            return votes, pct
    return None


# --- the header -------------------------------------------------------------


def identify_template(text: str) -> str | None:
    """Which recognised house format this is, if any."""

    lowered = str(text or "").lower()
    for template in TEMPLATES:
        if any(marker in lowered for marker in template.markers):
            return template.name
    return None


def consultant_hint(text: str) -> str | None:
    """Who wrote a document we do not recognise, in their own words.

    An unknown template is only useful if it can say what it is. Nationally
    the first question about a new state's corpus is "who produced these",
    and the documents answer it themselves in a line near the front.
    """

    for raw in str(text or "").splitlines()[:200]:
        match = _PREPARED_BY.search(dekern(raw))
        if match:
            name = re.sub(r"\s+", " ", match.group(1)).strip(" ,.")
            # A sentence continuing past the name is not a name.
            name = re.split(r"\b(?:in|for|on|with|and)\b", name)[0].strip(" ,.")
            if 3 < len(name) < 60:
                return name
    return None


@dataclass
class Header:
    hospital: str | None = None
    county: str | None = None
    state: str | None = None
    year: int | None = None
    cycle_label: str | None = None      # "Round #5", "Wave #4"
    consultant: str | None = None
    #: Who the document says produced it, when the format is unrecognised.
    #: The raw material for the next entry in TEMPLATES.
    consultant_hint: str | None = None
    townhall_date: str | None = None
    attendees: int | None = None
    total_votes: int | None = None

    @property
    def is_template(self) -> bool:
        return self.consultant is not None


_ON_BEHALF = re.compile(r"on\s+behalf\s+(?:of\s+)?(.+?)\s*$", re.IGNORECASE)
_COUNTY_STATE = re.compile(r"^\s*([A-Z][A-Za-z .'-]+?)\s+Co(?:unty)?\.?,\s*([A-Z]{2})\s*$")
_CYCLE = re.compile(r"\b(Round|Wave)\s*#?\s*(\d+)", re.IGNORECASE)
_YEAR = re.compile(r"\b(20\d{2})\b")

# "Town Hall - 04/04/24  (Attendees 27 / 108 Total Votes)"
# "CHNA Wave #4 Town Hall - 5/18/23   (29 Attendees / 88 Total Votes)"
_TOWNHALL = re.compile(
    r"Town\s*Hall\s*[-–]\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE
)
_ATTENDEES = re.compile(r"(?:Attendees\s*(\d+)|(\d+)\s*Attendees)", re.IGNORECASE)
_TOTAL_VOTES = re.compile(r"(\d+)\s*Total\s*Votes", re.IGNORECASE)
_TOTAL_ROW = re.compile(r"^\s*Total\s+Votes\s+(\d+)", re.IGNORECASE)


def parse_header(text: str) -> Header:
    """Read the title block and town hall line."""

    header = Header()
    lines = [dekern(raw) for raw in text.splitlines()[:400]]

    header.consultant = identify_template(text)
    if header.consultant is None:
        header.consultant_hint = consultant_hint(text)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if header.county is None:
            match = _COUNTY_STATE.match(stripped)
            if match:
                header.county, header.state = match.group(1).strip(), match.group(2)

        if header.hospital is None:
            match = _ON_BEHALF.search(stripped)
            if match:
                name = match.group(1).strip(" .,")
                # "on behalf Patterson Health, Anthony, KS" -- the town is not
                # part of the name.
                name = re.split(r",\s*[A-Za-z .]+,\s*[A-Z]{2}$", name)[0]
                if 3 < len(name) < 120:
                    header.hospital = name.strip(" ,")

        if header.cycle_label is None:
            match = _CYCLE.search(stripped)
            if match:
                header.cycle_label = f"{match.group(1).title()} #{match.group(2)}"

        match = _TOWNHALL.search(stripped)
        if match:
            header.townhall_date = match.group(1)
            attendees = _ATTENDEES.search(stripped)
            if attendees:
                header.attendees = int(attendees.group(1) or attendees.group(2))
            votes = _TOTAL_VOTES.search(stripped)
            if votes:
                header.total_votes = int(votes.group(1))

        if header.total_votes is None:
            match = _TOTAL_ROW.match(stripped)
            if match:
                header.total_votes = int(match.group(1))

        if header.year is None and re.search(r"CHNA|Assessment", stripped, re.I):
            match = _YEAR.search(stripped)
            if match:
                header.year = int(match.group(1))

    return header


# --- the priority tally -----------------------------------------------------


@dataclass
class Need:
    """One row of the ranked needs table."""

    rank: int
    label: str
    votes: int | None = None
    pct: float | None = None
    accum: float | None = None
    prior_rank: int | None = None
    #: True when the row had to be recovered from letter-spaced text.
    recovered: bool = False

    @property
    def moved(self) -> int | None:
        """Places gained since the previous cycle. Positive is worse."""

        if self.prior_rank is None:
            return None
        return self.rank - self.prior_rank


# "1 Substance Abuse (Drugs & Alcohol) 21 19.4% 19%"
_NEED_ROW = re.compile(
    r"^\s*(\d{1,2})\s+(.+?)\s+(\d{1,4})\s+(\d{1,3}(?:\.\d)?)\s*%\s+(\d{1,3}(?:\.\d)?)\s*%\s*$"
)
# "2 Mental Health Services (Access, Provider, Treatment, Aftercare) 80 10.4% 3"
_TREND_ROW = re.compile(
    r"^\s*(\d{1,2})\s+(.+?)\s+(\d{1,4})\s+(\d{1,3}(?:\.\d)?)\s*%\s+(\d{1,2})\s*$"
)
# A collapsed row: "6Home Health76.5%77%" after de-kerning.
_COLLAPSED = re.compile(
    r"^\s*(\d{1,2})([A-Za-z][A-Za-z ,&()/'.-]*?)(\d[\d.]*)%(\d{1,3}(?:\.\d)?)%\s*$"
)

_STOP_ROW = re.compile(r"^\s*(Total\s+Votes|Totals?)\b", re.IGNORECASE)


def _clean_label(raw: str) -> str:
    """Tidy a label without changing what it says."""

    label = re.sub(r"\s+", " ", raw).strip(" .,")
    # Extraction inserts spaces inside words: "Diagnos is", "prioriti sing".
    label = re.sub(r"(?<=[a-z]) (?=(is|es|ed|ing|ers|ion)\b)", "", label)
    # ...and before punctuation: "Diagnosis , Placement".
    label = re.sub(r"\s+([,;:)])", r"\1", label)
    return re.sub(r"\(\s+", "(", label)


def _join_wrapped(lines: list[str]) -> list[str]:
    """Rejoin table rows that wrapped mid-cell.

    A long need runs onto a second line, and the votes land at the end of the
    continuation. Joining a line to its predecessor when it cannot start a row
    of its own recovers those.
    """

    joined: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            joined.append(line)
            continue
        starts_row = re.match(r"^\s*\d{1,2}[\s A-Za-z]", line) is not None
        looks_like_tail = re.search(r"\d\s*%\s*(\d{1,3}(\.\d)?\s*%|\d{1,2})\s*$", stripped)
        if joined and looks_like_tail and not starts_row:
            joined[-1] = joined[-1].rstrip() + " " + stripped
        else:
            joined.append(line)
    return joined


def parse_needs(text: str, total_votes: int | None = None) -> list[Need]:
    """The ranked priority tally: what this community voted to change."""

    lines = _join_wrapped([dekern(raw) for raw in text.splitlines()])
    needs: list[Need] = []
    seen: set[int] = set()

    for line in lines:
        if _STOP_ROW.match(line):
            continue

        match = _NEED_ROW.match(line)
        if match:
            rank = int(match.group(1))
            if rank in seen or not 1 <= rank <= 30:
                continue
            seen.add(rank)
            needs.append(
                Need(
                    rank=rank,
                    label=_clean_label(match.group(2)),
                    votes=int(match.group(3)),
                    pct=float(match.group(4)),
                    accum=float(match.group(5)),
                )
            )
            continue

        match = _COLLAPSED.match(line)
        if match:
            rank = int(match.group(1))
            if rank in seen or not 1 <= rank <= 30:
                continue
            split = split_votes_and_pct(match.group(3), total_votes)
            if split is None:
                log.warning(
                    "CHNA: could not split %r for rank %d; recording the label only",
                    match.group(3),
                    rank,
                )
            seen.add(rank)
            needs.append(
                Need(
                    rank=rank,
                    label=_clean_label(match.group(2)),
                    votes=split[0] if split else None,
                    pct=split[1] if split else None,
                    accum=float(match.group(4)),
                    recovered=True,
                )
            )

    needs.sort(key=lambda n: n.rank)
    return needs


def parse_trend(text: str) -> list[Need]:
    """The ongoing-problem table, which carries each need's previous rank.

    This is the most valuable table in the document and the easiest to miss:
    the last column is not a percentage but where the same need sat in the
    previous cycle. A need that has been in the top five for three cycles
    without moving is a finding on its own.
    """

    lines = _join_wrapped([dekern(raw) for raw in text.splitlines()])
    rows: list[Need] = []
    seen: set[int] = set()

    for line in lines:
        if _STOP_ROW.match(line) or _NEED_ROW.match(line):
            continue
        match = _TREND_ROW.match(line)
        if not match:
            continue
        rank, prior = int(match.group(1)), int(match.group(5))
        if rank in seen or not 1 <= rank <= 40 or not 1 <= prior <= 40:
            continue
        seen.add(rank)
        rows.append(
            Need(
                rank=rank,
                label=_clean_label(match.group(2)),
                votes=int(match.group(3)),
                pct=float(match.group(4)),
                prior_rank=prior,
            )
        )

    rows.sort(key=lambda n: n.rank)
    return rows


# --- provider shortages -----------------------------------------------------

#: Abbreviations these documents actually use, alongside the full words. The
#: short forms are not guessable -- "Neu" and "Urol" come from one real line
#: reading "Derm, Ped, Ent, Eye, Neu, Ortho and Urol."
_SPECIALTIES: dict[str, tuple[str, ...]] = {
    "dermatology": ("dermatology", "dermatologist", "derm"),
    "pediatrics": ("pediatrics", "pediatrician", "ped", "peds"),
    "otolaryngology": ("otolaryngology", "ent"),
    "ophthalmology": ("ophthalmology", "ophthalmologist", "optometry", "eye"),
    "neurology": ("neurology", "neurologist", "neuro", "neu"),
    "orthopedics": ("orthopedics", "orthopaedics", "orthopedic", "ortho"),
    "urology": ("urology", "urologist", "urol"),
    "psychiatry": ("psychiatry", "psychiatrist", "psychiatric"),
    "cardiology": ("cardiology", "cardiologist", "cardiac"),
    "oncology": ("oncology", "oncologist"),
    # Not "prenatal care": every one of these documents reports the share of
    # births with first-trimester prenatal care as a health indicator, which is
    # a utilisation statistic and not a statement about staffing.
    "obstetrics": ("obstetrics", "obstetrician", "ob/gyn", "obgyn", "gynecology"),
    "general surgery": ("general surgery", "general surgeon"),
    "anesthesiology": ("anesthesiology", "anesthetist", "crna"),
    "radiology": ("radiology", "radiologist"),
    "podiatry": ("podiatry", "podiatrist"),
    "pulmonology": ("pulmonology", "pulmonologist"),
    "gastroenterology": ("gastroenterology", "gastroenterologist"),
    "nephrology": ("nephrology", "nephrologist"),
    "endocrinology": ("endocrinology", "endocrinologist"),
    "dentistry": ("dentist", "dental"),
    "primary care": ("primary care", "family medicine", "family practice"),
    "behavioral health": (
        "mental health provider", "mental health providers",
        "behavioral health provider", "counselor", "counselors",
        "substance abuse professional", "substance abuse professionals",
        "therapist", "therapists",
    ),
    "nursing": ("nurse", "nurses", "nursing", "rn", "lpn"),
    "ems": ("ems", "emt", "paramedic", "paramedics", "ambulance"),
    "pharmacy": ("pharmacist", "pharmacy"),
    "long term care": ("long term care", "long-term care"),
}

#: Words that turn a mention of a specialty into a claim about not having one.
_SHORTAGE_CUES = (
    "shortage", "lack of", "lacking", "unable to recruit", "difficult to recruit",
    "recruit", "recruitment", "need for additional", "additional",
    "without a", "vacancy", "vacancies", "staffing", "retention", "turnover",
)
# Deliberately not "visiting specialist": these documents print a roster of the
# visiting specialists a hospital already has, under a heading using those exact
# words, and it sits pages from the complaint about lacking them. As a cue it
# reported Cheyenne County short of anesthesiology on the strength of the
# anesthetist who visits every three weeks. "Lack of Visiting Specialists: Derm,
# Ped, ..." still lands, because "lack of" is doing the work there anyway.

_WORD_BOUNDARY = "[^a-z]"


def _sentences(text: str):
    """Sentences, with the document's line wrapping undone first.

    Never split on a colon. The single most valuable line in this corpus is
    ``Lack of Visiting Specialists: Derm, Ped, Ent, Eye, Neu, Ortho and
    Urol.`` — the colon separates the statement that they are short of
    specialists from the list of which ones, and splitting there loses the
    connection between them entirely. A single newline is treated the same
    way, because that list is wrapped across two lines in the original.
    """

    for paragraph in re.split(r"\n\s*\n", text):
        flat = re.sub(r"\s*\n\s*", " ", dekern(paragraph.replace("\n", " \n")))
        flat = re.sub(r"\s+", " ", flat)
        for sentence in re.split(r"(?<=[.!?])\s+", flat):
            cleaned = sentence.strip()
            if cleaned:
                yield cleaned


@dataclass
class Shortage:
    """A specialty the hospital says it does not have enough of."""

    specialty: str
    verbatim: str
    #: True when the sentence also says they are trying to fill it.
    recruiting: bool = False


#: How close a shortage cue must sit to the specialty it is about.
#:
#: Sentence boundaries were the first attempt and they do not exist in these
#: documents. A CHNA is mostly tables, tables carry no full stops, and the
#: "sentence" that results is a page. Within a page-long span some cue always
#: appears near some specialty, so the extractor reported a hospital short of
#: anesthesiology on the strength of "Keith Gist, CRNA" -- a roster of the
#: visiting specialists it *has* -- and short of nursing and pharmacy from a
#: patient-satisfaction table.
#:
#: Proximity is the honest constraint. "Lack of Visiting Specialists: Derm,
#: Ped, Ent, Eye, Neu, Ortho and Urol." spans about seventy characters end to
#: end, so a window this size keeps the real thing and drops the coincidences.
PROXIMITY = 110


def find_shortages(text: str, *, context: int = 200) -> list[Shortage]:
    """Specialties named close to a statement of being short of one.

    A CHNA lists every service the hospital *has* alongside every one it
    wants — often in adjacent tables — so matching a specialty alone reports
    a hospital as short of the very thing it advertises. Requiring a cue
    nearby is what separates "Lack of Visiting Specialists: ... Urol." from
    "Wichita Urology, 1st and 4th Thursday", which is a urologist who already
    visits.
    """

    flat = re.sub(r"\s+", " ", dekern(str(text or "").replace("\n", " ")))
    lowered = flat.lower()

    cue_spans = [
        (m.start(), m.end(), cue)
        for cue in _SHORTAGE_CUES
        for m in re.finditer(re.escape(cue), lowered)
    ]
    if not cue_spans:
        return []
    cue_spans.sort()

    found: dict[str, Shortage] = {}
    for canonical, aliases in _SPECIALTIES.items():
        for alias in aliases:
            pattern = f"{_WORD_BOUNDARY}{re.escape(alias)}{_WORD_BOUNDARY}"
            for hit in re.finditer(pattern, lowered):
                near = [
                    (start, end, cue)
                    for start, end, cue in cue_spans
                    if start < hit.end() + PROXIMITY and end > hit.start() - PROXIMITY
                ]
                if not near:
                    continue
                recruiting = any(
                    cue in ("recruit", "recruitment") for _s, _e, cue in near
                )
                low = min(min(s for s, _e, _c in near), hit.start())
                verbatim = flat[max(0, low - 20) : low + context].strip()
                existing = found.get(canonical)
                if existing is None:
                    found[canonical] = Shortage(
                        specialty=canonical, verbatim=verbatim, recruiting=recruiting
                    )
                elif recruiting and not existing.recruiting:
                    existing.recruiting = True
                    existing.verbatim = verbatim
                break
            if canonical in found:
                break

    return sorted(found.values(), key=lambda s: s.specialty)


# --- the whole document -----------------------------------------------------


@dataclass
class Assessment:
    header: Header
    needs: list[Need] = field(default_factory=list)
    trend: list[Need] = field(default_factory=list)
    shortages: list[Shortage] = field(default_factory=list)
    declines_boilerplate: int = 0

    @property
    def template(self) -> str | None:
        return self.header.consultant

    @property
    def recovered_rows(self) -> int:
        """Rows that only survived because of de-kerning."""

        return sum(1 for n in self.needs if n.recovered)


def parse_assessment(text: str) -> Assessment:
    """Read one assessment end to end."""

    header = parse_header(text)
    assessment = Assessment(
        header=header,
        needs=parse_needs(text, header.total_votes),
        trend=parse_trend(text),
        shortages=find_shortages(text),
        declines_boilerplate=len(DECLINE_BOILERPLATE.findall(text)),
    )
    log.info(
        "CHNA: %s (%s, %s) — %d need(s), %d trend row(s), %d shortage(s)%s",
        header.hospital or "unknown hospital",
        header.county or "unknown county",
        header.cycle_label or header.year or "no cycle",
        len(assessment.needs),
        len(assessment.trend),
        len(assessment.shortages),
        f", {assessment.recovered_rows} row(s) recovered from kerned text"
        if assessment.recovered_rows
        else "",
    )
    return assessment


# --- reading a file ---------------------------------------------------------


def read_pdf(path: str) -> str:
    """Text of a PDF, with page breaks marked.

    Raises ``ValueError`` naming the document when nothing extracts, which
    means it is a scan and needs OCR — a different job with a different cost,
    and one worth reporting rather than silently loading as an empty file.
    """

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValueError("reading CHNA PDFs needs pypdf (pip install pypdf)") from exc

    reader = PdfReader(path)
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n\n".join(pages)
    if len(text.strip()) < 150 * max(len(pages), 1):
        raise ValueError(
            f"{path}: {len(text.strip())} characters from {len(pages)} page(s) — "
            "this is almost certainly a scan and needs OCR"
        )
    return text


def read_document(path: str) -> str:
    """Text of a CHNA, whether it arrives as a PDF or as extracted text."""

    if str(path).lower().endswith(".pdf"):
        return read_pdf(path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()
