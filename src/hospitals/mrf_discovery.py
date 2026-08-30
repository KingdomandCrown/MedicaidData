"""Finding a hospital's machine-readable file, from its own website.

CMS requires every hospital to publish ``cms-hpt.txt`` at the root of its
domain, naming the location and giving the URL of its standard-charges file.
That makes discovery mechanical: given a website, the MRF address is one HTTP
request away, and no scraping or guesswork is involved.

The reason this is a separate step from parsing is a measurement. The existing
JavaScript crawler does both, and on a Texas/Oklahoma/Louisiana run it reached
41 hospitals and kept 12: 22 "no MRF", 12 "0 services", 7 parse timeouts. The
Python parser in this package has read 1,030 files across every layout CMS
allows — tall CSV, wide CSV, JSON, XLSX, zipped, gzipped, mis-encoded. So the
failure is not in reaching hospitals; it is in parsing what was reached, twice,
in two languages, one of which is much better at it.

Discovery writes a manifest. Downloading reads it. Ingestion reads the files.
Each step can be inspected, retried, and corrected by hand, which matters
because the interesting cases are the ones a crawler should not decide alone.

**Ambiguity is recorded, not resolved.** A health system publishes one
``cms-hpt.txt`` listing every facility it owns, and picking the wrong block
attributes one hospital's prices to another — a mistake that is invisible
afterwards, because the file that arrives looks perfectly valid. Where the
name match is not clear, every candidate goes into the manifest with the
status ``ambiguous`` and a person picks. The JavaScript crawler skips these
silently, which is where a good share of its 22 "no MRF" results came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .logging_config import get_logger

log = get_logger(__name__)

#: How much of two names must overlap before a location block is taken as a
#: match. Below this the candidates are recorded and nobody is picked.
MATCH_THRESHOLD = 0.34

#: Words that appear in so many hospital names they carry no signal. Without
#: this "Memorial Hospital" matches "Community Memorial Medical Center" on the
#: strength of the word "memorial".
_STOPWORDS = frozenset(
    """
    inc llc lp the of and at dba co corp corporation company
    hospital hospitals medical center centre health healthcare
    regional memorial community district county clinic clinics campus
    system systems services service group associates
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")

# "location-name: X", "Location_Name = X". CMS's own examples use hyphens; real
# files use both, and some use "=" rather than ":".
_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]*?)\s*[:=]\s*(.*)$")


def significant_tokens(name: str | None) -> set[str]:
    """The words in a hospital name that distinguish it from another one."""

    words = _WORD_RE.findall(str(name or "").lower())
    kept = {w for w in words if len(w) > 2 and w not in _STOPWORDS}
    # A name made entirely of stopwords ("Community Hospital") still has to
    # match something, so fall back to the full word list rather than nothing.
    return kept or {w for w in words if len(w) > 2}


def name_similarity(a: str | None, b: str | None) -> float:
    """Jaccard overlap of the distinguishing words. 0.0 when either is empty."""

    left, right = significant_tokens(a), significant_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass
class HptRecord:
    """One facility block from a ``cms-hpt.txt`` file."""

    mrf_url: str
    location_name: str | None = None
    fields: dict = field(default_factory=dict)


def parse_cms_hpt(body: str) -> list[HptRecord]:
    """Read the repeating blocks of a ``cms-hpt.txt`` file.

    The format has no block delimiter — a new facility is signalled only by a
    ``location-name`` appearing when one has already been seen. Line endings
    are whatever the hospital's web server produced.
    """

    records: list[HptRecord] = []
    current: dict = {}

    def flush() -> None:
        url = current.get("mrf-url")
        if url:
            records.append(
                HptRecord(
                    mrf_url=url.strip(),
                    location_name=(current.get("location-name") or "").strip() or None,
                    fields=dict(current),
                )
            )

    for raw in str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _FIELD_RE.match(raw)
        if not match:
            continue
        key = match.group(1).strip().lower().replace("_", "-")
        value = match.group(2).strip()
        if key == "location-name" and "location-name" in current:
            flush()
            current = {}
        current[key] = value

    flush()
    return records


@dataclass
class Match:
    """Which block a hospital's name picked, and how confident that is."""

    record: HptRecord | None
    score: float
    alternatives: list[HptRecord] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        return self.record is not None and self.score >= MATCH_THRESHOLD


def _score(hospital_name: str | None, city: str | None, record: HptRecord) -> float:
    """How well one block matches, with the city counted if we know it.

    A large system's facilities differ by place, not by name: Kaiser publishes
    dozens of "Kaiser Foundation Hospital" blocks and the word that separates
    them is Fresno or Anaheim. Comparing the name alone scores every one of
    them identically and correctly refuses to choose. Adding the city is what
    turns "no idea" into an answer.

    The higher of the two scores wins, so a location-name that omits the city
    is not penalised for it.
    """

    plain = name_similarity(hospital_name, record.location_name)
    if not city:
        return plain
    return max(plain, name_similarity(f"{hospital_name} {city}", record.location_name))


def _names_the_city(record: HptRecord, city: str | None) -> bool:
    if not city:
        return False
    return bool(significant_tokens(city) & significant_tokens(record.location_name))


def match_location(
    hospital_name: str | None,
    records: list[HptRecord],
    city: str | None = None,
) -> Match:
    """Pick the block belonging to one hospital.

    A file with a single block needs no matching — a hospital publishing one
    MRF at its own domain is the ordinary case, and demanding a name match
    there would reject correct files over spelling.
    """

    usable = [r for r in records if r.mrf_url]
    if not usable:
        return Match(None, 0.0)
    if len(usable) == 1:
        return Match(usable[0], 1.0)

    scored = sorted(
        ((_score(hospital_name, city, r), r) for r in usable),
        key=lambda pair: -pair[0],
    )
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    # A tie between two facilities is not a match, however high it scores. On a
    # system domain the runner-up is a different real hospital — unless exactly
    # one of the tied blocks names this hospital's city, which is precisely the
    # fact that tells Kaiser Fresno from Kaiser Anaheim.
    if best_score >= MATCH_THRESHOLD and best_score > runner_up:
        return Match(best, best_score, [r for _, r in scored[1:]])

    if best_score >= MATCH_THRESHOLD and city:
        tied = [r for score, r in scored if score >= best_score - 1e-9]
        naming = [r for r in tied if _names_the_city(r, city)]
        if len(naming) == 1:
            rest = [r for _, r in scored if r is not naming[0]]
            return Match(naming[0], best_score, rest)

    return Match(None, best_score, [r for _, r in scored])


def site_root(website: str | None) -> str:
    """The bare hostname a ``cms-hpt.txt`` would live at."""

    raw = str(website or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname or ""
    return host.lower().removeprefix("www.")


def hpt_urls(root: str) -> list[str]:
    """Where to look, in order. CMS names the file; servers vary on case."""

    if not root:
        return []
    return [
        f"https://{root}/cms-hpt.txt",
        f"https://www.{root}/cms-hpt.txt",
        f"https://{root}/CMS-HPT.txt",
    ]


@dataclass
class Discovery:
    """One hospital's result, whether or not anything was found."""

    ccn: str
    name: str
    state: str | None = None
    website: str | None = None
    mrf_url: str | None = None
    location_name: str | None = None
    status: str = "no_website"
    note: str = ""
    score: float | None = None

    @property
    def is_actionable(self) -> bool:
        return self.status == "found" and bool(self.mrf_url)


def _absolute(mrf_url: str, from_page: str) -> str:
    """A relative ``mrf-url`` is legal and several hospitals publish one."""

    return urljoin(from_page, mrf_url.strip())


def discover_one(
    hospital: dict,
    fetch,
    *,
    cache: dict | None = None,
) -> list[Discovery]:
    """Look one hospital up. Returns one row, or several when ambiguous.

    ``fetch(url) -> str | None`` returns the body or None. ``cache`` maps a
    site root to the parsed records, because a system's twenty hospitals share
    one ``cms-hpt.txt`` and fetching it twenty times is both slow and rude.
    """

    ccn = str(hospital.get("ccn") or "").strip()
    name = str(hospital.get("name") or "").strip()
    state = hospital.get("state")
    website = hospital.get("website")
    city = hospital.get("city")
    base = Discovery(ccn=ccn, name=name, state=state, website=website)

    root = site_root(website)
    if not root:
        base.status = "no_website"
        base.note = "no website on record for this hospital"
        return [base]

    if cache is not None and root in cache:
        records, page = cache[root]
    else:
        records, page = [], None
        for url in hpt_urls(root):
            body = fetch(url)
            if not body:
                continue
            # A 200 that is really the site's 404 page is common enough to be
            # the default outcome rather than an edge case.
            if "mrf-url" not in body.lower() and "mrf_url" not in body.lower():
                continue
            parsed = parse_cms_hpt(body)
            if parsed:
                records, page = parsed, url
                break
        if cache is not None:
            cache[root] = (records, page)

    if not records:
        base.status = "no_hpt"
        base.note = f"no readable cms-hpt.txt at {root}"
        return [base]

    match = match_location(name, records, hospital.get("city"))
    if match.is_confident:
        base.status = "found"
        base.mrf_url = _absolute(match.record.mrf_url, page or f"https://{root}/")
        base.location_name = match.record.location_name
        base.score = round(match.score, 3)
        return [base]

    # A system file with no clear winner. Every candidate is written out so a
    # person can pick, rather than a crawler guessing and attributing one
    # hospital's negotiated rates to another.
    rows = []
    for record in (match.alternatives or records)[:12]:
        row = Discovery(
            ccn=ccn,
            name=name,
            state=state,
            website=website,
            mrf_url=_absolute(record.mrf_url, page or f"https://{root}/"),
            location_name=record.location_name,
            status="ambiguous",
            score=round(name_similarity(name, record.location_name), 3),
            note=(
                f"{len(records)} facilities share {root}; "
                f"best name match scored {match.score:.2f}, below {MATCH_THRESHOLD}"
            ),
        )
        rows.append(row)
    rows.sort(key=lambda r: -(r.score or 0))
    return rows


MANIFEST_COLUMNS = (
    "ccn",
    "name",
    "state",
    "status",
    "mrf_url",
    "location_name",
    "score",
    "website",
    "note",
)


def to_row(discovery: Discovery) -> dict:
    return {
        "ccn": discovery.ccn,
        "name": discovery.name,
        "state": discovery.state or "",
        "status": discovery.status,
        "mrf_url": discovery.mrf_url or "",
        "location_name": discovery.location_name or "",
        "score": "" if discovery.score is None else f"{discovery.score:.3f}",
        "website": discovery.website or "",
        "note": discovery.note,
    }
