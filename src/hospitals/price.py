"""What a billing code costs, across every hospital in the database.

The store holds item × payer × plan negotiated rates for hundreds of
hospitals. Everything built so far has been about getting data *in* and
attributing it correctly; this is the first thing that asks it a question.

Three prices answer "what does this cost", and they are not close to each
other:

* **gross charge** — the chargemaster list price, which almost nobody pays.
* **discounted cash** — what a self-pay patient is quoted.
* **negotiated** — what a specific payer actually pays, and the only one that
  differs by insurer.

The median matters more than the mean here. Chargemaster prices have a long
right tail — one hospital listing a complete blood count at $400 drags an
average that no patient experiences — so every figure reported is a
percentile, and the count behind it is reported alongside so a thin sample
cannot pass for a national answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .db import charge_sources, hospitals, standard_charges
from .logging_config import get_logger

log = get_logger(__name__)


def percentile(values: list[float], p: float) -> float | None:
    """The ``p``-th percentile by linear interpolation (type 7).

    The same method NumPy and R use by default, so a figure quoted from here
    matches one computed anywhere else.
    """

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@dataclass
class PriceSpread:
    """The distribution of one kind of price."""

    label: str
    values: list[float] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def median(self) -> float | None:
        return percentile(self.values, 50)

    @property
    def p25(self) -> float | None:
        return percentile(self.values, 25)

    @property
    def p75(self) -> float | None:
        return percentile(self.values, 75)

    @property
    def low(self) -> float | None:
        return min(self.values) if self.values else None

    @property
    def high(self) -> float | None:
        return max(self.values) if self.values else None


@dataclass
class PayerSpread:
    payer: str
    values: list[float] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def median(self) -> float | None:
        return percentile(self.values, 50)


@dataclass
class PriceReport:
    code: str
    descriptions: dict[str, int] = field(default_factory=dict)
    hospitals_seen: set[str] = field(default_factory=set)
    rows: int = 0
    gross: PriceSpread = field(default_factory=lambda: PriceSpread("gross charge"))
    cash: PriceSpread = field(default_factory=lambda: PriceSpread("discounted cash"))
    negotiated: PriceSpread = field(default_factory=lambda: PriceSpread("negotiated"))
    payers: dict[str, PayerSpread] = field(default_factory=dict)
    state: str | None = None

    @property
    def hospital_count(self) -> int:
        return len(self.hospitals_seen)

    @property
    def common_description(self) -> str | None:
        if not self.descriptions:
            return None
        return max(self.descriptions.items(), key=lambda kv: kv[1])[0]

    @property
    def top_payers(self) -> list[PayerSpread]:
        return sorted(self.payers.values(), key=lambda p: -p.count)


def _as_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def price_for_code(
    engine: Engine,
    code: str,
    *,
    state: str | None = None,
    payer: str | None = None,
    linked_only: bool = True,
) -> PriceReport:
    """Collect every price recorded for one billing code.

    ``linked_only`` counts a hospital only where the file was attributed to a
    CCN, so "across N hospitals" means N identified hospitals rather than N
    files of unknown provenance.
    """

    report = PriceReport(code=code, state=state)

    stmt = (
        select(
            standard_charges.c.description,
            standard_charges.c.gross_charge,
            standard_charges.c.discounted_cash,
            standard_charges.c.negotiated_dollar,
            standard_charges.c.payer_name,
            charge_sources.c.ccn,
            charge_sources.c.source_file,
        )
        .select_from(
            standard_charges.join(
                charge_sources, standard_charges.c.source_id == charge_sources.c.id
            )
        )
        .where(standard_charges.c.code == code)
    )

    if linked_only:
        stmt = stmt.where(charge_sources.c.ccn.is_not(None))
    if state:
        stmt = stmt.join(
            hospitals, hospitals.c.ccn == charge_sources.c.ccn
        ).where(hospitals.c.state == state.upper())
    if payer:
        stmt = stmt.where(func.lower(standard_charges.c.payer_name).like(f"%{payer.lower()}%"))

    with engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            report.rows += 1
            key = row["ccn"] or row["source_file"]
            if key:
                report.hospitals_seen.add(key)

            description = (row["description"] or "").strip()
            if description:
                report.descriptions[description] = report.descriptions.get(description, 0) + 1

            gross = _as_float(row["gross_charge"])
            cash = _as_float(row["discounted_cash"])
            negotiated = _as_float(row["negotiated_dollar"])

            # A zero or negative price is a placeholder, not a price.
            if gross and gross > 0:
                report.gross.values.append(gross)
            if cash and cash > 0:
                report.cash.values.append(cash)
            if negotiated and negotiated > 0:
                report.negotiated.values.append(negotiated)
                name = (row["payer_name"] or "").strip() or "(unnamed)"
                report.payers.setdefault(name, PayerSpread(name)).values.append(negotiated)

    log.info(
        "Code %s: %d row(s) across %d hospital(s); %d negotiated, %d cash, %d gross",
        code,
        report.rows,
        report.hospital_count,
        report.negotiated.count,
        report.cash.count,
        report.gross.count,
    )
    return report
