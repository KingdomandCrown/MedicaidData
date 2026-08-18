"""Link price-transparency files to Provider-of-Services hospitals.

The two data sources use different primary keys: the POS ``hospitals`` table is
keyed by **CCN**, while a price-transparency ``charge_sources`` row is keyed by
the hospital's **organizational NPI** and **EIN**. There is no CCN in the MRF
schema, so joining them needs a bridge.

The authoritative bridge is an **NPI -> CCN crosswalk** (loaded from a CSV you
supply — see ``load_crosswalk``). Where no crosswalk entry exists, an optional
**name + state** heuristic attempts a match against the POS hospitals and is
recorded with a lower-confidence method label so it can be audited or overridden
later.

Each linked ``charge_sources`` row gets its ``ccn`` and ``link_method``
populated (``crosswalk_npi`` or ``name_state``).
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from .db import charge_sources, hospitals, npi_ccn_crosswalk
from .logging_config import get_logger

log = get_logger(__name__)

# Legal-entity noise words dropped before comparing hospital names.
_NAME_NOISE = {
    "THE", "INC", "LLC", "LP", "LLP", "PC", "PA", "CORP", "CORPORATION",
    "CO", "COMPANY", "OF", "AND",
}

_STATE_IN_ADDRESS = re.compile(r",\s*([A-Za-z]{2})\s+\d{5}")


def normalize_name(name: str | None) -> str:
    """Normalize a hospital name for heuristic matching."""

    if not name:
        return ""
    text = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    tokens = [t for t in text.split() if t and t not in _NAME_NOISE]
    return " ".join(tokens)


def _state_of(source_row) -> str | None:
    if source_row["license_state"]:
        return source_row["license_state"].upper()
    match = _STATE_IN_ADDRESS.search(source_row["hospital_address"] or "")
    return match.group(1).upper() if match else None


@dataclass
class LinkSummary:
    total: int
    by_crosswalk: int
    by_name: int
    unlinked: int


# --- crosswalk loading ----------------------------------------------------


def load_crosswalk(engine: Engine, csv_path: str, source: str = "manual") -> int:
    """Load an NPI->CCN crosswalk CSV into ``npi_ccn_crosswalk``.

    The CSV must have (case-insensitive) ``npi`` and ``ccn`` columns; an
    optional ``name`` column is stored for reference. Existing NPIs are
    replaced. Returns the number of rows loaded.
    """

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        field_map = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
        if "npi" not in field_map or "ccn" not in field_map:
            raise ValueError(
                f"crosswalk CSV must have 'npi' and 'ccn' columns; got {reader.fieldnames}"
            )
        for raw in reader:
            npi = (raw.get(field_map["npi"]) or "").strip()
            ccn = (raw.get(field_map["ccn"]) or "").strip().upper()
            if not npi or not ccn:
                continue
            name = None
            if "name" in field_map:
                name = (raw.get(field_map["name"]) or "").strip() or None
            rows.append(
                {"npi": npi, "ccn": ccn, "name": name, "source": source, "loaded_at": now}
            )

    if not rows:
        log.warning("Crosswalk CSV %s had no usable rows", csv_path)
        return 0

    written = write_crosswalk_rows(engine, rows)
    log.info("Loaded %d NPI->CCN crosswalk rows from %s", written, csv_path)
    return written


def write_crosswalk_rows(engine: Engine, rows: list[dict], batch_size: int = 500) -> int:
    """Replace crosswalk entries for the NPIs in ``rows``.

    Shared by the CSV loader and the CMS fetcher so both write the table the
    same way. Batched because ``IN (...)`` with tens of thousands of NPIs is
    not a query any engine enjoys.
    """

    if not rows:
        return 0

    with engine.begin() as conn:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            conn.execute(
                delete(npi_ccn_crosswalk).where(
                    npi_ccn_crosswalk.c.npi.in_([r["npi"] for r in batch])
                )
            )
            conn.execute(insert(npi_ccn_crosswalk), batch)
    return len(rows)


# --- linking --------------------------------------------------------------


def link_charges(engine: Engine, use_name_fallback: bool = True) -> LinkSummary:
    """Populate ``charge_sources.ccn`` from the crosswalk (+ optional heuristic).

    Returns a :class:`LinkSummary` of how many were linked by each method.
    """

    with engine.begin() as conn:
        crosswalk = {
            row.npi: row.ccn
            for row in conn.execute(
                select(npi_ccn_crosswalk.c.npi, npi_ccn_crosswalk.c.ccn)
            )
        }

        # Build a (normalized_name, state) -> set(ccn) index of POS hospitals.
        name_index: dict[tuple[str, str], set[str]] = {}
        for h in conn.execute(
            select(hospitals.c.ccn, hospitals.c.name, hospitals.c.state)
        ):
            if not h.ccn or not h.state:
                continue
            key = (normalize_name(h.name), h.state.upper())
            if key[0]:
                name_index.setdefault(key, set()).add(h.ccn)

        sources = conn.execute(
            select(
                charge_sources.c.id,
                charge_sources.c.hospital_name,
                charge_sources.c.npis,
                charge_sources.c.primary_npi,
                charge_sources.c.license_state,
                charge_sources.c.hospital_address,
            )
        ).mappings().all()

        by_crosswalk = by_name = 0
        for src in sources:
            ccn = None
            method = None

            # ``npis`` is the pipe-joined list from the file's metadata;
            # ``primary_npi`` may have been filled in from the filename for a
            # file that carried none. Either is a legitimate identifier.
            candidates_npi = (src["npis"] or "").split("|")
            if src["primary_npi"]:
                candidates_npi.append(src["primary_npi"])
            for npi in candidates_npi:
                npi = npi.strip()
                if npi and npi in crosswalk:
                    ccn, method = crosswalk[npi], "crosswalk_npi"
                    break

            if ccn is None and use_name_fallback:
                state = _state_of(src)
                if state:
                    key = (normalize_name(src["hospital_name"]), state)
                    candidates = name_index.get(key)
                    if candidates and len(candidates) == 1:
                        ccn, method = next(iter(candidates)), "name_state"

            if ccn is not None:
                conn.execute(
                    charge_sources.update()
                    .where(charge_sources.c.id == src["id"])
                    .values(ccn=ccn, link_method=method)
                )
                if method == "crosswalk_npi":
                    by_crosswalk += 1
                else:
                    by_name += 1

        total = len(sources)

    unlinked = total - by_crosswalk - by_name
    log.info(
        "Linked %d/%d charge sources (crosswalk=%d, name+state=%d, unlinked=%d)",
        by_crosswalk + by_name,
        total,
        by_crosswalk,
        by_name,
        unlinked,
    )
    return LinkSummary(
        total=total,
        by_crosswalk=by_crosswalk,
        by_name=by_name,
        unlinked=unlinked,
    )
