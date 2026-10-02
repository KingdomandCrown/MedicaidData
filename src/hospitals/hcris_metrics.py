"""Turn raw HCRIS worksheet cells into named financial/operational metrics.

Applies the Form CMS-2552-10 crosswalk in ``hcris_crosswalk.py`` to
``hcris_numeric``/``hcris_alpha``, producing one value per (report, metric).
This is the "judgment call" layer the raw ``hcris_fetch`` pipeline
deliberately stayed out of -- but the judgment belongs to that upstream,
published crosswalk, not to a guess made here: a metric's value is exactly
what the matching worksheet/line/column cell(s) say, summed when the
crosswalk names a line range, nothing else asserted.

A report missing a metric's worksheet/line/column -- the hospital didn't file
that section, or the cell was blank -- simply gets no row for that metric.
There is no zero-filling: a missing value and a reported zero are not the
same fact, and collapsing them would silently misstate every ratio built on
top of this.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from .db import hcris_alpha, hcris_metric_values, hcris_numeric, hcris_reports
from .hcris_crosswalk import CROSSWALK, CrosswalkEntry
from .logging_config import get_logger

log = get_logger(__name__)

# Cell key: (wksht_cd, int(clmn_num), int(line_num)) -> value. Int conversion
# makes "00100" and "100" the same cell regardless of exact zero-padding.
_CellMap = dict[tuple[str, int, int], object]


@dataclass
class MaterializeSummary:
    reports_processed: int = 0
    values_written: int = 0


def _entries(include_disabled: bool) -> list[CrosswalkEntry]:
    return [e for e in CROSSWALK if include_disabled or e.enabled]


def _index_cells(rows) -> dict[int, _CellMap]:
    """Group (report_id, wksht_cd, clmn_num, line_num, value) rows by report_id."""

    by_report: dict[int, _CellMap] = defaultdict(dict)
    for report_id, wksht_cd, clmn_num, line_num, value in rows:
        try:
            key = (wksht_cd, int(clmn_num), int(line_num))
        except (TypeError, ValueError):
            continue  # a blank or non-numeric line/column code; not a cell we can place
        by_report[report_id][key] = value
    return by_report


def _resolve_numeric(entry: CrosswalkEntry, cells: _CellMap) -> Decimal | None:
    clmn = int(entry.clmn_num)
    start = int(entry.line_start)
    end = int(entry.line_end) if entry.line_end is not None else start

    total: Decimal | None = None
    for line in range(start, end + 1):
        value = cells.get((entry.wksht_cd, clmn, line))
        if value is None:
            continue
        total = (total or Decimal(0)) + Decimal(value)
    return total


def _resolve_alpha(entry: CrosswalkEntry, cells: _CellMap) -> str | None:
    clmn = int(entry.clmn_num)
    line = int(entry.line_start)  # no alpha entry in this crosswalk uses a range
    value = cells.get((entry.wksht_cd, clmn, line))
    return str(value) if value is not None else None


def compute_report_metrics(
    engine: Engine, report_id: int, *, include_disabled: bool = False
) -> dict[str, Decimal | str]:
    """Named metric values for one report, computed live from the raw tables.

    Only metrics that actually had matching data come back -- a metric this
    report never filed is simply absent from the result, not set to None.
    """

    entries = _entries(include_disabled)
    numeric_entries = [e for e in entries if e.metric_type != "alpha"]
    alpha_entries = [e for e in entries if e.metric_type == "alpha"]
    result: dict[str, Decimal | str] = {}

    with engine.connect() as conn:
        if numeric_entries:
            wksht_cds = sorted({e.wksht_cd for e in numeric_entries})
            rows = conn.execute(
                select(
                    hcris_numeric.c.report_id, hcris_numeric.c.wksht_cd,
                    hcris_numeric.c.clmn_num, hcris_numeric.c.line_num, hcris_numeric.c.value,
                ).where(
                    hcris_numeric.c.report_id == report_id,
                    hcris_numeric.c.wksht_cd.in_(wksht_cds),
                )
            ).all()
            cells = _index_cells(rows).get(report_id, {})
            for entry in numeric_entries:
                value = _resolve_numeric(entry, cells)
                if value is not None:
                    result[entry.metric] = value

        if alpha_entries:
            wksht_cds = sorted({e.wksht_cd for e in alpha_entries})
            rows = conn.execute(
                select(
                    hcris_alpha.c.report_id, hcris_alpha.c.wksht_cd,
                    hcris_alpha.c.clmn_num, hcris_alpha.c.line_num, hcris_alpha.c.value,
                ).where(
                    hcris_alpha.c.report_id == report_id,
                    hcris_alpha.c.wksht_cd.in_(wksht_cds),
                )
            ).all()
            cells = _index_cells(rows).get(report_id, {})
            for entry in alpha_entries:
                value = _resolve_alpha(entry, cells)
                if value is not None:
                    result[entry.metric] = value

    return result


def materialize_metrics(
    engine: Engine,
    *,
    vintage_year: int,
    include_disabled: bool = False,
    batch_size: int = 5000,
) -> MaterializeSummary:
    """Compute every crosswalk metric for every report in one vintage and
    store the results in hcris_metric_values, replacing any prior run for
    that vintage. One vintage per transaction, like fetch_year: an
    interrupted run leaves nothing behind to be mistaken for a finished one.
    """

    entries = _entries(include_disabled)
    numeric_entries = [e for e in entries if e.metric_type != "alpha"]
    alpha_entries = [e for e in entries if e.metric_type == "alpha"]
    summary = MaterializeSummary()

    with engine.begin() as conn:
        report_ids = [
            row[0]
            for row in conn.execute(
                select(hcris_reports.c.id).where(hcris_reports.c.vintage_year == vintage_year)
            )
        ]
        if not report_ids:
            return summary

        numeric_cells: dict[int, _CellMap] = {}
        if numeric_entries:
            wksht_cds = sorted({e.wksht_cd for e in numeric_entries})
            rows = conn.execute(
                select(
                    hcris_numeric.c.report_id, hcris_numeric.c.wksht_cd,
                    hcris_numeric.c.clmn_num, hcris_numeric.c.line_num, hcris_numeric.c.value,
                )
                .select_from(
                    hcris_numeric.join(hcris_reports, hcris_numeric.c.report_id == hcris_reports.c.id)
                )
                .where(
                    hcris_reports.c.vintage_year == vintage_year,
                    hcris_numeric.c.wksht_cd.in_(wksht_cds),
                )
            )
            numeric_cells = _index_cells(rows)

        alpha_cells: dict[int, _CellMap] = {}
        if alpha_entries:
            wksht_cds = sorted({e.wksht_cd for e in alpha_entries})
            rows = conn.execute(
                select(
                    hcris_alpha.c.report_id, hcris_alpha.c.wksht_cd,
                    hcris_alpha.c.clmn_num, hcris_alpha.c.line_num, hcris_alpha.c.value,
                )
                .select_from(
                    hcris_alpha.join(hcris_reports, hcris_alpha.c.report_id == hcris_reports.c.id)
                )
                .where(
                    hcris_reports.c.vintage_year == vintage_year,
                    hcris_alpha.c.wksht_cd.in_(wksht_cds),
                )
            )
            alpha_cells = _index_cells(rows)

        # A correlated subquery, not a literal list of report_ids -- a vintage
        # can carry several thousand reports, past what some drivers accept
        # as individually-bound IN() parameters.
        conn.execute(
            delete(hcris_metric_values).where(
                hcris_metric_values.c.report_id.in_(
                    select(hcris_reports.c.id).where(hcris_reports.c.vintage_year == vintage_year)
                )
            )
        )

        batch: list[dict] = []
        for report_id in report_ids:
            summary.reports_processed += 1
            cells = numeric_cells.get(report_id, {})
            for entry in numeric_entries:
                value = _resolve_numeric(entry, cells)
                if value is None:
                    continue
                batch.append({"report_id": report_id, "metric": entry.metric,
                               "value_numeric": value, "value_text": None})
            a_cells = alpha_cells.get(report_id, {})
            for entry in alpha_entries:
                value = _resolve_alpha(entry, a_cells)
                if value is None:
                    continue
                batch.append({"report_id": report_id, "metric": entry.metric,
                               "value_numeric": None, "value_text": value})
            if len(batch) >= batch_size:
                conn.execute(insert(hcris_metric_values), batch)
                summary.values_written += len(batch)
                batch = []
        if batch:
            conn.execute(insert(hcris_metric_values), batch)
            summary.values_written += len(batch)

    log.info(
        "HCRIS metrics vintage %d: %d report(s), %d value(s) written",
        vintage_year, summary.reports_processed, summary.values_written,
    )
    return summary
