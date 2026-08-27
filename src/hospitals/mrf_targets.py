"""Which hospitals to crawl, and where their websites come from.

The database knows every hospital and which ones already have a charge file.
It does not know their websites — that lives in the scorecard app's
``hospital-info.json``, which carries 3,794 of them. Joining the two is what
turns "620 of 7,031 covered" into a work list with an address on every row.

Nothing here reaches the network. It answers "who is worth asking", so a run
can be scoped, counted, and reasoned about before a single request is made.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .db import charge_sources, hospitals
from .logging_config import get_logger

log = get_logger(__name__)

#: Where the scorecard app keeps its hospital profiles.
DEFAULT_INFO_PATH = os.path.expanduser("~/minerva-4.0/hospital-info.json")


def load_websites(path: str) -> dict[str, str]:
    """CCN -> website, from ``hospital-info.json``.

    Accepts both shapes the file has had: a top-level ``hospitals`` object, or
    the mapping itself. A CCN is a string key there; JSON would have dropped
    the leading zero of a Connecticut hospital had it been a number.
    """

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    table = data.get("hospitals", data) if isinstance(data, dict) else {}
    websites: dict[str, str] = {}
    for ccn, profile in table.items():
        if not isinstance(profile, dict):
            continue
        web = (profile.get("web") or profile.get("website") or "").strip()
        if web:
            websites[str(ccn).strip().upper()] = web
    log.info("Loaded %d website(s) from %s", len(websites), path)
    return websites


@dataclass
class Target:
    ccn: str
    name: str
    state: str | None
    website: str | None

    def as_dict(self) -> dict:
        return {
            "ccn": self.ccn,
            "name": self.name,
            "state": self.state,
            "website": self.website,
        }


@dataclass
class TargetSummary:
    """What a run would attempt, and what it would leave out and why."""

    in_scope: int = 0
    already_covered: int = 0
    no_website: int = 0
    targets: list[Target] = None

    def __post_init__(self):
        if self.targets is None:
            self.targets = []


def choose_targets(
    engine: Engine,
    websites: dict[str, str],
    *,
    states: list[str] | None = None,
    include_covered: bool = False,
    limit: int | None = None,
) -> TargetSummary:
    """The hospitals worth asking, newest gaps first.

    Hospitals that already have a linked charge file are skipped: this is for
    filling the 6,411-hospital hole, not re-downloading the 620 we hold. A
    hospital with no website on record is counted separately rather than
    dropped silently, because that count is the ceiling on what any crawler
    can ever reach and it should be visible.
    """

    wanted = {s.strip().upper() for s in (states or []) if s.strip()}
    summary = TargetSummary()

    with engine.connect() as conn:
        covered = {
            row.ccn
            for row in conn.execute(
                select(charge_sources.c.ccn).where(charge_sources.c.ccn.is_not(None))
            )
        }

        stmt = select(hospitals.c.ccn, hospitals.c.name, hospitals.c.state)
        if wanted:
            stmt = stmt.where(hospitals.c.state.in_(sorted(wanted)))
        stmt = stmt.order_by(hospitals.c.state, hospitals.c.name)

        for row in conn.execute(stmt):
            if not row.ccn:
                continue
            summary.in_scope += 1
            if row.ccn in covered and not include_covered:
                summary.already_covered += 1
                continue
            website = websites.get(str(row.ccn).strip().upper())
            if not website:
                summary.no_website += 1
                continue
            summary.targets.append(
                Target(ccn=row.ccn, name=row.name or "", state=row.state, website=website)
            )

    if limit is not None:
        summary.targets = summary.targets[:limit]

    log.info(
        "%d hospital(s) in scope: %d already covered, %d with no website, %d to try",
        summary.in_scope,
        summary.already_covered,
        summary.no_website,
        len(summary.targets),
    )
    return summary
