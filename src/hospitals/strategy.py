"""Reading what a hospital promised to do about it.

The assessment says what a community needs. The **implementation strategy**
says what the hospital will do — and, because Treasury regulations demand it,
what it will not. That second half is the part nobody reads and the part worth
the most: a public, dated, sometimes costed list of problems the hospital has
declined to take on, with its stated reason.

These are harder to read than the assessments. A CHNA is a report; a strategy
is a spreadsheet, and a spreadsheet flattened into PDF text loses its columns.
Patterson Health Center's plan has Lead, Partners, Timeframe, Hours and Dollars
as five columns, and in the extracted text they run together with the action
description in an order that changes from row to row.

So this does not pretend to recover the columns. It takes the two things that
survive flattening intact and are worth the most anyway:

* **money and hours**, which sit at the end of a row in a fixed shape
* **the decline**, which is a sentence, not a cell

and keeps the rest of the row verbatim for a person to read. A parser that
guessed at column boundaries would produce a tidy table of plausible nonsense,
which is worse than an untidy one that is right.

One discrimination matters enough to name. Not every dollar figure is a
commitment: Olathe's plan says its county's average per capita income is
$56,364 in a paragraph describing the need, three pages from the $50,000 a year
it commits to a police co-responder salary. One is a promise and one is a
statistic about the county, and adding them together would be worse than
reading neither.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .chna import DECLINE_BOILERPLATE, dekern
from .logging_config import get_logger

log = get_logger(__name__)

# "Ongoing 4 $320", "2025 $1,700", "2026 2 $1,100", "Ongoing 186 $7,400".
# Timeframe and hours are both optional because rows omit them freely.
_COST_TAIL = re.compile(
    r"(?:(Ongoing|\d{4}(?:\s*[-–]\s*Year\s*\d+)?)\s+)?(\d{1,4})?\s*\$\s?([\d,]+)\s*$",
    re.IGNORECASE,
)

# "Overall Total Contributions 256 $23,250"
_TOTAL_LINE = re.compile(
    r"\b(?:overall\s+)?total\s+contributions?\b.*?(\d{1,5})\s+\$\s?([\d,]+)",
    re.IGNORECASE,
)

# A need heading in the plan, which arrives in three shapes:
#
#   "1 Substance Abuse (Drugs & Alcohol) a"          number, label, tactic letter
#   "3 EMS (Staffing, Coverage, Funding) a Continue to support..."   ...and the
#                                                    first tactic run onto it
#   "2"                                              number alone, label wrapped
#                                                    onto the following lines
#
# Getting this wrong is not cosmetic: every tactic below a missed heading is
# filed under the previous need, so the hospital appears to have promised one
# thing about a problem it was addressing somewhere else entirely.
_NEED_HEADING = re.compile(
    r"^\s*(\d{1,2})\s+([A-Z][A-Za-z0-9 ,&()/'’.-]{3,70}?)"
    r"(?:\s+([a-z])(?:\s+[A-Z].*)?)?\s*$"
)
_LONE_NUMBER = re.compile(r"^\s*(\d{1,2})\s*$")
# The wrapped label ends where the first tactic begins.
_LABEL_END = re.compile(r"^\s*[a-z]\s")

#: Verbs that make a dollar figure a promise rather than an observation.
_COMMITTING = (
    "contribute", "fund", "funding", "provide", "subsidize", "subsidise",
    "invest", "allocate", "commit", "donate", "sponsor", "support",
    "purchase", "pay for", "cover the cost",
)

#: Phrases that make a dollar figure a fact about the county instead.
# Deliberately not "salary": subsidising one is the single largest concrete
# commitment in this corpus ("$50,000 annually to subsidize the salary of the
# behavioral health specialist employed through the Olathe Police Department").
# A cue that vetoes the best example of what we are looking for is the wrong cue.
_DESCRIPTIVE = (
    "per capita", "median", "average income", "household income", "poverty",
    "cost of living", "unemployment", "revenue", "budget deficit",
)

_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")

# A tactic in the VVV plan: an indented single letter, then a capitalised verb.
_TACTIC = re.compile(r"^\s*([a-z])\s+([A-Z].*)$")

#: Fiscal-year columns in the health-system format ("FY 2025 FY 2026 FY 2027").
_FISCAL = re.compile(r"\bFY\s?(20\d{2})\b", re.IGNORECASE)


def _amount(raw: str) -> int | None:
    try:
        return int(float(raw.replace(",", "")))
    except (TypeError, ValueError):
        return None


@dataclass
class Commitment:
    """One thing the hospital said it would do.

    ``text`` is the row as extracted, not a cleaned action description: the
    lead and partner columns are in there too, because the flattened text does
    not say where one ends and the next begins.
    """

    text: str
    need_rank: int | None = None
    letter: str | None = None
    timeframe: str | None = None
    hours: int | None = None
    dollars: int | None = None
    fiscal_years: list[str] = field(default_factory=list)
    #: True when the money came from a tabulated cost column rather than prose.
    tabulated: bool = False


@dataclass
class Strategy:
    needs: list[tuple[int, str]] = field(default_factory=list)
    commitments: list[Commitment] = field(default_factory=list)
    declines: int = 0
    stated_total_hours: int | None = None
    stated_total_dollars: int | None = None

    @property
    def committed_dollars(self) -> int:
        """Summed from the rows. Compare against the plan's own total."""

        return sum(c.dollars or 0 for c in self.commitments)

    @property
    def committed_hours(self) -> int:
        return sum(c.hours or 0 for c in self.commitments)

    @property
    def reconciles(self) -> bool | None:
        """Whether our sum matches the total the plan printed for itself.

        ``None`` when the plan states no total. A mismatch is not necessarily
        our error — plans have arithmetic mistakes — but it is the one check
        available, and a silent disagreement is worth surfacing.
        """

        if self.stated_total_dollars is None:
            return None
        return self.committed_dollars == self.stated_total_dollars


def _is_costed_row(line: str, cost: re.Match) -> bool:
    """Whether a line ending in a dollar figure is a costed commitment.

    The Wyandotte County assessment charts household income, and its axis
    labels extract as a column of bare amounts -- ``$700``, ``$750``, ``$800``
    -- each of which ends a line with a dollar figure and nothing else. Read
    as commitments they summed to $123,100 of promises that hospital never
    made, next to a real total of zero. A number that looks like a budget and
    is not is worse than no number.

    A real row from these plans carries a timeframe or an hour count beside
    the money. Failing both, it needs enough words to be a description and a
    verb that makes it a promise.
    """

    if cost.group(1) or cost.group(2):      # a timeframe or an hour count
        return True
    prefix = line[: cost.start()]
    letters = sum(ch.isalpha() for ch in prefix)
    return letters >= 12 and _is_commitment_sentence(line)


def _is_commitment_sentence(sentence: str) -> bool:
    lowered = f" {sentence.lower()} "
    if any(cue in lowered for cue in _DESCRIPTIVE):
        return False
    return any(cue in lowered for cue in _COMMITTING)


def parse_strategy(text: str) -> Strategy:
    """Read an implementation strategy."""

    strategy = Strategy()
    lines = [dekern(raw) for raw in text.splitlines()]
    current_need: int | None = None
    pending_label: list[str] | None = None
    # Needs are numbered 1..N in order, so the next heading is the only one
    # worth accepting -- which is also what keeps stray numbers out.
    last_rank = 0
    saw_full_heading = False

    def remember(rank: int, label: str) -> None:
        if label and not any(r == rank for r, _ in strategy.needs):
            strategy.needs.append((rank, re.sub(r"\s+", " ", label).strip(" ,")))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # A label that wrapped after a lone number, still being collected.
        if pending_label is not None:
            if _LABEL_END.match(line) or _MONEY.search(stripped):
                remember(current_need, " ".join(pending_label))
                pending_label = None
            elif len(pending_label) < 4 and not _LONE_NUMBER.match(stripped):
                pending_label.append(stripped)
                continue
            else:
                remember(current_need, " ".join(pending_label))
                pending_label = None

        total = _TOTAL_LINE.search(stripped)
        if total:
            strategy.stated_total_hours = _amount(total.group(1))
            strategy.stated_total_dollars = _amount(total.group(2))
            continue

        # A bare number only introduces a need in the tabular plan, and only
        # as the next one in sequence. Slide decks number their pages, and
        # without both guards a 21-page deck yields twenty-one "needs" whose
        # labels are whatever sentence happened to follow the page number.
        lone = _LONE_NUMBER.match(stripped)
        if lone and saw_full_heading and int(lone.group(1)) == last_rank + 1:
            current_need = last_rank = int(lone.group(1))
            pending_label = []
            continue

        heading = _NEED_HEADING.match(stripped)
        if (
            heading
            and not _MONEY.search(stripped)
            and int(heading.group(1)) == last_rank + 1
        ):
            saw_full_heading = True
            current_need = last_rank = int(heading.group(1))
            remember(current_need, heading.group(2))
            continue

        cost = _COST_TAIL.search(stripped)
        if cost and _MONEY.search(stripped) and _is_costed_row(stripped, cost):
            letter = None
            tactic = _TACTIC.match(stripped)
            if tactic:
                letter = tactic.group(1)
            strategy.commitments.append(
                Commitment(
                    text=re.sub(r"\s+", " ", stripped),
                    need_rank=current_need,
                    letter=letter,
                    timeframe=(cost.group(1) or "").strip() or None,
                    hours=_amount(cost.group(2)) if cost.group(2) else None,
                    dollars=_amount(cost.group(3)),
                    fiscal_years=sorted(set(_FISCAL.findall(stripped))),
                    tabulated=True,
                )
            )
            continue

        # Prose money: a promise only when the sentence makes it one.
        if _MONEY.search(stripped) and _is_commitment_sentence(stripped):
            amount = _MONEY.search(stripped)
            strategy.commitments.append(
                Commitment(
                    text=re.sub(r"\s+", " ", stripped),
                    need_rank=current_need,
                    dollars=_amount(amount.group(1)),
                    fiscal_years=sorted(set(_FISCAL.findall(stripped))),
                    tabulated=False,
                )
            )

    strategy.declines = len(DECLINE_BOILERPLATE.findall(text))

    log.info(
        "Strategy: %d need(s), %d costed commitment(s) totalling $%s, "
        "%d boilerplate decline(s)%s",
        len(strategy.needs),
        len(strategy.commitments),
        f"{strategy.committed_dollars:,}",
        strategy.declines,
        ""
        if strategy.reconciles is not False
        else f" — plan states ${strategy.stated_total_dollars:,}, rows sum to "
        f"${strategy.committed_dollars:,}",
    )
    return strategy
