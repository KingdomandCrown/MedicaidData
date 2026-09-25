"""Download a CMS HCRIS cost-report release and store it raw, by vintage.

This is deliberately the mechanical half of cost-report ingestion, not the
interpretive half. CMS publishes one release per quarter (``HOSP10FY<year>``),
each a full cumulative re-publication of every hospital's Form CMS-2552-10
cost report back to 2010 — not a delta — and each shows "the highest level of
status" available as of that release, so a report can move from as-submitted
to settled between releases. Rather than guess whether a later release's
figures for the same report should replace an earlier one, nothing is ever
overwritten: every vintage is stored side by side, keyed by
``(rpt_rec_num, vintage_year)``. Trending is preserved by construction.

The Report file's own column layout is not decoded beyond RPT_REC_NUM, the one
field every documented source agrees on — everything else in that row is kept
as the untouched original line rather than asserted into named fields that
might be wrong. Numeric and Alpha both use CMS's well-established, universally
documented 5-column shape (RPT_REC_NUM, WKSHT_CD, LINE_NUM, CLMN_NUM, VALUE),
so those are decoded directly. Turning WKSHT_CD/LINE_NUM/CLMN_NUM into named
financial metrics is a judgment call for later, not this module.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import itertools
import os
import zipfile
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlparse

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from .db import hcris_alpha, hcris_numeric, hcris_reports
from .logging_config import get_logger
from .mrf_fetch import requests_opener
from .price_transparency import clean_text, detect_delimiter, detect_encoding, to_decimal

log = get_logger(__name__)

BASE_URL_TEMPLATE = "https://downloads.cms.gov/files/hcris/HOSP10FY{year}.ZIP"
EARLIEST_VINTAGE_YEAR = 2010

#: A HCRIS annual release zip runs to a few hundred MB. Past this it is a
#: mistake or a redirect to something else, not a bigger release.
MAX_ZIP_BYTES = 4 * 1024 * 1024 * 1024

_SNIFF_BYTES = 256 * 1024

#: Keyword-matched rather than exact-matched: the real member names inside
#: each year's zip are not verified against CMS's own documentation, and a
#: brittle exact match would fail silently on a rename. A wrong or missing
#: match fails loudly instead, in _classify_members below.
_ROLE_KEYWORDS = {
    "report": ("RPT",),
    "numeric": ("NMRC", "NUMERIC"),
    "alpha": ("ALPHA",),
}


class HcrisLayoutError(RuntimeError):
    """The zip's members don't match the expected report/numeric/alpha shape."""


@dataclass
class HcrisFetchSummary:
    vintage_year: int
    status: str = "error"  # loaded | already_loaded | error
    zip_path: str | None = None
    reports_loaded: int = 0
    numeric_loaded: int = 0
    alpha_loaded: int = 0
    orphan_numeric: int = 0
    orphan_alpha: int = 0
    note: str = ""


def _classify_members(names: Sequence[str]) -> dict[str, str]:
    """Find the report/numeric/alpha member in a HCRIS release zip.

    Raises loudly, listing every member, when a role is missing or ambiguous
    rather than guess — an unattended run must stop and be looked at, not
    silently skip a file it could not identify.
    """

    roles: dict[str, str] = {}
    for name in names:
        upper = os.path.basename(name).upper()
        if not upper.endswith((".CSV", ".TXT")):
            continue
        for role, keywords in _ROLE_KEYWORDS.items():
            if any(keyword in upper for keyword in keywords):
                if role in roles:
                    raise HcrisLayoutError(
                        f"more than one {role!r} member in the zip: "
                        f"{roles[role]!r} and {name!r}"
                    )
                roles[role] = name

    missing = [role for role in _ROLE_KEYWORDS if role not in roles]
    if missing:
        raise HcrisLayoutError(
            f"could not find a {', '.join(missing)} member in the zip; "
            f"members present: {sorted(names)}"
        )
    return roles


def _download(url: str, cache_dir: str, *, opener, max_bytes: int) -> str:
    """Download ``url`` into ``cache_dir``, reusing a prior download as-is.

    A HCRIS zip does not change once published, so a file already on disk
    with a non-zero size is trusted outright — this is what makes re-running
    an interrupted fetch cheap instead of a multi-GB re-download.
    """

    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, os.path.basename(urlparse(url).path))
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log.info("Using cached download: %s", dest)
        return dest

    _content_type, chunks = opener(url)
    partial = f"{dest}.{os.getpid()}.part"
    written = 0
    try:
        with open(partial, "wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"{url}: exceeded {max_bytes} bytes; stopped rather than fill the disk"
                    )
                handle.write(chunk)
    except Exception:
        _remove(partial)
        raise
    if written == 0:
        _remove(partial)
        raise ValueError(f"{url}: server returned an empty body")

    os.replace(partial, dest)
    log.info("Downloaded %s (%.1f MB)", dest, written / 1e6)
    return dest


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _text_stream(zf: zipfile.ZipFile, member: str):
    """A decoded text stream over a zip member, encoding sniffed per member."""

    with zf.open(member) as probe:
        encoding = detect_encoding(probe.read(_SNIFF_BYTES))
    if encoding != "utf-8-sig":
        log.info("%s: reading as %s", member, encoding)
    raw = zf.open(member)
    return io.TextIOWrapper(raw, encoding=encoding, newline="", errors="replace")


def _load_report_file(conn, zf: zipfile.ZipFile, member: str, *, vintage_year: int,
                       source_zip: str, batch_size: int) -> int:
    """Insert every report row, decoding only RPT_REC_NUM; the rest is kept verbatim."""

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    loaded = 0
    with _text_stream(zf, member) as text:
        first_line = text.readline()
        delimiter = detect_delimiter(first_line)
        batch: list[dict] = []
        for line in itertools.chain([first_line], text):
            line = line.rstrip("\r\n")
            if not line:
                continue
            first_field = line.split(delimiter, 1)[0].strip().strip('"')
            try:
                rpt_rec_num = int(first_field)
            except ValueError:
                continue
            batch.append(
                {
                    "rpt_rec_num": rpt_rec_num,
                    "vintage_year": vintage_year,
                    "raw_line": line,
                    "source_zip": source_zip,
                    "ingested_at": now,
                }
            )
            if len(batch) >= batch_size:
                conn.execute(insert(hcris_reports), batch)
                loaded += len(batch)
                batch = []
        if batch:
            conn.execute(insert(hcris_reports), batch)
            loaded += len(batch)
    return loaded


def _load_numeric_or_alpha(conn, zf: zipfile.ZipFile, member: str, table, report_map: dict[int, int],
                            *, is_numeric: bool, batch_size: int) -> tuple[int, int]:
    """Insert every row of a Numeric or Alpha member, keyed to its report's row id.

    A row whose RPT_REC_NUM isn't in this vintage's report file is counted as
    an orphan and skipped rather than aborting the whole release over it —
    real HCRIS data has stray rows like this, and an unattended multi-hour
    run should finish and report the count, not stop on one bad line.
    """

    loaded = orphans = 0
    with _text_stream(zf, member) as text:
        first_line = text.readline()
        delimiter = detect_delimiter(first_line)
        reader = csv.reader(itertools.chain([first_line], text), delimiter=delimiter)
        batch: list[dict] = []
        for row in reader:
            if len(row) < 5:
                continue
            try:
                rpt_rec_num = int(row[0].strip())
            except ValueError:
                continue
            report_id = report_map.get(rpt_rec_num)
            if report_id is None:
                orphans += 1
                continue
            value = to_decimal(row[4]) if is_numeric else clean_text(row[4])
            batch.append(
                {
                    "report_id": report_id,
                    "wksht_cd": clean_text(row[1]),
                    "line_num": clean_text(row[2]),
                    "clmn_num": clean_text(row[3]),
                    "value": value,
                }
            )
            if len(batch) >= batch_size:
                conn.execute(insert(table), batch)
                loaded += len(batch)
                batch = []
        if batch:
            conn.execute(insert(table), batch)
            loaded += len(batch)
    return loaded, orphans


def fetch_year(
    engine: Engine,
    vintage_year: int,
    *,
    cache_dir: str,
    opener=None,
    max_bytes: int = MAX_ZIP_BYTES,
    batch_size: int = 5000,
) -> HcrisFetchSummary:
    """Download one HCRIS vintage and load its raw rows, unless already loaded.

    A vintage is loaded inside one transaction, so an interrupted run leaves
    nothing behind for that vintage rather than a half-loaded one a re-run
    would then skip — checking "any report rows already stored for this
    vintage" is enough to make re-running the whole command safe.
    """

    summary = HcrisFetchSummary(vintage_year=vintage_year)

    with engine.connect() as conn:
        already = conn.execute(
            select(func.count())
            .select_from(hcris_reports)
            .where(hcris_reports.c.vintage_year == vintage_year)
        ).scalar_one()
    if already:
        summary.status = "already_loaded"
        summary.reports_loaded = already
        summary.note = (
            f"{already:,} report row(s) already stored for vintage {vintage_year}; "
            "skipping (nothing to resume — a prior run finished)"
        )
        log.info(summary.note)
        return summary

    url = BASE_URL_TEMPLATE.format(year=vintage_year)
    opener = opener or requests_opener(timeout=300)
    summary.zip_path = _download(url, cache_dir, opener=opener, max_bytes=max_bytes)
    source_zip = os.path.basename(summary.zip_path)

    with zipfile.ZipFile(summary.zip_path) as zf:
        roles = _classify_members(zf.namelist())

        with engine.begin() as conn:
            summary.reports_loaded = _load_report_file(
                conn, zf, roles["report"],
                vintage_year=vintage_year, source_zip=source_zip, batch_size=batch_size,
            )

            report_map = {
                rpt_rec_num: report_id
                for report_id, rpt_rec_num in conn.execute(
                    select(hcris_reports.c.id, hcris_reports.c.rpt_rec_num).where(
                        hcris_reports.c.vintage_year == vintage_year
                    )
                )
            }

            summary.numeric_loaded, summary.orphan_numeric = _load_numeric_or_alpha(
                conn, zf, roles["numeric"], hcris_numeric, report_map,
                is_numeric=True, batch_size=batch_size,
            )
            summary.alpha_loaded, summary.orphan_alpha = _load_numeric_or_alpha(
                conn, zf, roles["alpha"], hcris_alpha, report_map,
                is_numeric=False, batch_size=batch_size,
            )

    summary.status = "loaded"
    log.info(
        "HCRIS vintage %d: %d report row(s), %d numeric row(s) (%d orphaned), "
        "%d alpha row(s) (%d orphaned)",
        vintage_year, summary.reports_loaded, summary.numeric_loaded, summary.orphan_numeric,
        summary.alpha_loaded, summary.orphan_alpha,
    )
    return summary
