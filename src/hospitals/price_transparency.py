"""Parse CMS Hospital Price Transparency machine-readable files (MRF).

Since 2024, hospitals must publish a "standard charges" file in the CMS schema
(currently version 3.x). Two physical layouts of the same schema exist in the
wild, and this module reads both:

* **tall**  — one row per item x payer x plan. ``payer_name`` / ``plan_name``
  are their own columns. (e.g. Johns Hopkins)
* **wide**  — one row per item, with each payer/plan spread across its own set
  of columns like ``standard_charge|AETNA [1003]|AETNA PPO [100307]|negotiated_dollar``.
  These are unpivoted back into item x payer x plan rows. (e.g. U-Michigan)

Every MRF begins with two metadata rows (hospital name, address, NPI, license,
version, last-updated) followed by the data header and then the data. Parsing
is driven entirely by header names, because column *order* varies between
hospitals.

The file itself may be a plain ``.csv`` or a ``.zip`` containing one CSV; both
are streamed, never fully extracted, so a 340 MB file stays memory- and
disk-friendly.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterator, Sequence

from .logging_config import get_logger
from .normalize import clean_str, parse_date

log = get_logger(__name__)

# Allow the very wide header rows / long fields these files can contain.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Per-payer subfields that hang off ``standard_charge|payer|plan|<subfield>``.
_PAYER_CHARGE_SUBFIELDS = {
    "negotiated_dollar",
    "negotiated_percentage",
    "negotiated_algorithm",
    "methodology",
}
# Per-payer metric columns of the form ``<metric>|payer|plan``.
_PAYER_METRIC_FIELDS = {
    "median_amount",
    "10th_percentile",
    "90th_percentile",
    "count",
    "additional_payer_notes",
}


@dataclass
class MrfMetadata:
    """Hospital-level metadata parsed from the first two rows of an MRF."""

    hospital_name: str | None
    location_name: str | None
    hospital_address: str | None
    version: str | None
    last_updated_on: dt.date | None
    npis: list[str] = field(default_factory=list)
    license_number: str | None = None
    license_state: str | None = None
    ein: str | None = None
    source_file: str | None = None
    layout: str | None = None  # "tall" | "wide"

    @property
    def primary_npi(self) -> str | None:
        return self.npis[0] if self.npis else None


@dataclass
class ChargeRow:
    """A single normalized standard-charge fact (item x payer x plan)."""

    description: str | None
    code: str | None
    code_type: str | None
    additional_codes: str | None  # "type:code;type:code" for code|2..code|N
    billing_class: str | None
    setting: str | None
    drug_unit_of_measurement: str | None
    drug_type_of_measurement: str | None
    modifiers: str | None
    gross_charge: Decimal | None
    discounted_cash: Decimal | None
    min_charge: Decimal | None
    max_charge: Decimal | None
    payer_name: str | None
    plan_name: str | None
    negotiated_dollar: Decimal | None
    negotiated_percentage: Decimal | None
    negotiated_algorithm: str | None
    methodology: str | None
    median_amount: Decimal | None
    percentile_10: Decimal | None
    percentile_90: Decimal | None
    count: int | None
    additional_notes: str | None


# --- value parsing --------------------------------------------------------


def to_decimal(value) -> Decimal | None:
    text = clean_str(value)
    if text is None:
        return None
    text = text.replace("$", "").replace(",", "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def to_int(value) -> int | None:
    text = clean_str(value)
    if text is None:
        return None
    text = text.replace(",", "")
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return None


# --- filename / EIN -------------------------------------------------------

_EIN_RE = re.compile(r"(\d{9})")


def ein_from_filename(path: str) -> str | None:
    """Extract the 9-digit EIN from a CMS-convention MRF filename.

    CMS names files ``<ein>_<hospital-name>_standardcharges.<ext>``. Uploads
    sometimes prepend a hex hash (``<hash>-<ein>_...``); we strip that first.
    """

    base = os.path.basename(path)
    base = re.sub(r"^[0-9a-f]{6,}-", "", base, count=1)  # drop upload hash
    match = _EIN_RE.match(base) or _EIN_RE.search(base)
    return match.group(1) if match else None


# --- file opening ---------------------------------------------------------


def open_mrf_text(path: str):
    """Return a text stream for an MRF ``.csv`` or ``.zip`` (context manager).

    For a zip, the first ``.csv`` member is streamed without extraction.
    """

    if path.lower().endswith(".zip"):
        return _ZipCsvStream(path)
    return open(path, "r", encoding="utf-8-sig", newline="")


class _ZipCsvStream:
    """Context manager streaming the first CSV inside a zip as text."""

    def __init__(self, path: str):
        self._zip = zipfile.ZipFile(path)
        members = [n for n in self._zip.namelist() if n.lower().endswith(".csv")]
        if not members:
            self._zip.close()
            raise ValueError(f"no CSV member found inside {path}")
        self._member = members[0]
        self._raw = self._zip.open(self._member)
        self._text = io.TextIOWrapper(self._raw, encoding="utf-8-sig", newline="")

    def __enter__(self):
        return self._text

    def __exit__(self, *exc):
        self._text.close()
        self._raw.close()
        self._zip.close()


# --- metadata + layout ----------------------------------------------------


def _index_map(header: Sequence[str]) -> dict[str, int]:
    return {h.strip().lower(): i for i, h in enumerate(header)}


def parse_metadata(meta_header: Sequence[str], meta_values: Sequence[str]) -> MrfMetadata:
    idx = _index_map(meta_header)

    def val(name: str) -> str | None:
        i = idx.get(name)
        if i is None or i >= len(meta_values):
            return None
        return clean_str(meta_values[i])

    npi_raw = val("type_2_npi") or ""
    npis = [n for n in re.split(r"[|,/;\s]+", npi_raw) if n.strip().isdigit()]

    # License column is named ``license_number|<STATE>``. The value itself is
    # sometimes quoted and may embed the state again (e.g. '"1060000024|MI"').
    license_number = None
    license_state = None
    for header_name, i in idx.items():
        if header_name.startswith("license_number"):
            if "|" in header_name:
                license_state = header_name.split("|", 1)[1].upper()
            raw = clean_str(meta_values[i]) if i < len(meta_values) else None
            if raw:
                raw = raw.strip().strip('"').strip()
                if "|" in raw:
                    number, embedded_state = raw.split("|", 1)
                    raw = number.strip()
                    license_state = license_state or embedded_state.strip().upper()
            license_number = raw or None
            break

    return MrfMetadata(
        hospital_name=val("hospital_name"),
        location_name=val("location_name"),
        hospital_address=val("hospital_address"),
        version=val("version"),
        last_updated_on=parse_date(val("last_updated_on")),
        npis=npis,
        license_number=license_number,
        license_state=license_state,
    )


def detect_layout(data_header: Sequence[str]) -> str:
    """Return "tall" or "wide" from the data header column names."""

    lowered = [c.strip().lower() for c in data_header]
    if "payer_name" in lowered:
        return "tall"
    # Wide files have per-payer columns with 4 pipe-separated segments.
    for col in lowered:
        parts = col.split("|")
        if parts[0] == "standard_charge" and len(parts) == 4:
            return "wide"
    # Default to tall (single-payer / item-only files).
    return "tall"


# --- payer column grammar (wide) -----------------------------------------


def _parse_payer_column(col: str):
    """Parse a wide per-payer column into ``(payer, plan, field)`` or None."""

    parts = col.split("|")
    if parts[0] == "standard_charge" and len(parts) == 4:
        _sc, payer, plan, subfield = parts
        if subfield in _PAYER_CHARGE_SUBFIELDS:
            return payer, plan, subfield
        return None
    if parts[0] in _PAYER_METRIC_FIELDS and len(parts) == 3:
        metric, payer, plan = parts
        return payer, plan, metric
    return None


def _group_payer_columns(header: Sequence[str]) -> dict[tuple[str, str], dict[str, int]]:
    groups: dict[tuple[str, str], dict[str, int]] = {}
    for i, col in enumerate(header):
        parsed = _parse_payer_column(col.strip())
        if parsed:
            payer, plan, fieldname = parsed
            groups.setdefault((payer, plan), {})[fieldname] = i
    return groups


# --- item-level extraction ------------------------------------------------


def _collect_codes(row: Sequence[str], idx: dict[str, int]) -> tuple[str | None, str | None, str | None]:
    """Return (primary_code, primary_type, additional 'type:code;...')."""

    codes: list[tuple[str, str]] = []
    n = 1
    while f"code|{n}" in idx:
        code = clean_str(row[idx[f"code|{n}"]]) if idx[f"code|{n}"] < len(row) else None
        type_idx = idx.get(f"code|{n}|type")
        ctype = clean_str(row[type_idx]) if type_idx is not None and type_idx < len(row) else None
        if code:
            codes.append((code, ctype or ""))
        n += 1
    if not codes:
        return None, None, None
    primary_code, primary_type = codes[0]
    extra = ";".join(f"{t}:{c}" for c, t in codes[1:]) or None
    return primary_code, primary_type or None, extra


def _cell(row: Sequence[str], idx: dict[str, int], name: str):
    i = idx.get(name)
    if i is None or i >= len(row):
        return None
    return row[i]


# --- top-level reader -----------------------------------------------------


def read_mrf(path: str, limit: int | None = None) -> tuple[MrfMetadata, Iterator[ChargeRow]]:
    """Open an MRF and return ``(metadata, charge_row_iterator)``.

    ``limit`` caps the number of *items* (data rows) read, which is handy for
    smoke-testing very large files.
    """

    stream_cm = open_mrf_text(path)
    stream = stream_cm.__enter__()
    reader = csv.reader(stream)

    try:
        meta_header = next(reader)
        meta_values = next(reader)
        data_header = next(reader)
    except StopIteration as exc:
        stream_cm.__exit__(None, None, None)
        raise ValueError(f"{path}: file ended before the data header") from exc

    metadata = parse_metadata(meta_header, meta_values)
    metadata.ein = ein_from_filename(path)
    metadata.source_file = re.sub(
        r"^[0-9a-f]{6,}-", "", os.path.basename(path), count=1
    )
    metadata.layout = detect_layout(data_header)
    log.info(
        "MRF %s: %s (%s layout, NPI=%s, EIN=%s, version=%s)",
        metadata.source_file,
        metadata.hospital_name,
        metadata.layout,
        metadata.primary_npi,
        metadata.ein,
        metadata.version,
    )

    idx = _index_map(data_header)
    payer_groups = (
        _group_payer_columns(data_header) if metadata.layout == "wide" else {}
    )

    def _iter() -> Iterator[ChargeRow]:
        try:
            emitted = 0
            for n_items, row in enumerate(reader, start=1):
                if metadata.layout == "wide":
                    for charge in _wide_rows(row, idx, payer_groups):
                        emitted += 1
                        yield charge
                else:
                    charge = _tall_row(row, idx)
                    if charge is not None:
                        emitted += 1
                        yield charge
                if limit is not None and n_items >= limit:
                    break
            log.info("Parsed %d charge rows from %s", emitted, metadata.source_file)
        finally:
            stream_cm.__exit__(None, None, None)

    return metadata, _iter()


def _item_fields(row: Sequence[str], idx: dict[str, int]) -> dict:
    code, code_type, extra = _collect_codes(row, idx)
    return dict(
        description=clean_str(_cell(row, idx, "description")),
        code=code,
        code_type=code_type,
        additional_codes=extra,
        billing_class=clean_str(_cell(row, idx, "billing_class")),
        setting=clean_str(_cell(row, idx, "setting")),
        drug_unit_of_measurement=clean_str(_cell(row, idx, "drug_unit_of_measurement")),
        drug_type_of_measurement=clean_str(_cell(row, idx, "drug_type_of_measurement")),
        modifiers=clean_str(_cell(row, idx, "modifiers")),
        gross_charge=to_decimal(_cell(row, idx, "standard_charge|gross")),
        discounted_cash=to_decimal(_cell(row, idx, "standard_charge|discounted_cash")),
        min_charge=to_decimal(_cell(row, idx, "standard_charge|min")),
        max_charge=to_decimal(_cell(row, idx, "standard_charge|max")),
    )


def _tall_row(row: Sequence[str], idx: dict[str, int]) -> ChargeRow | None:
    base = _item_fields(row, idx)
    if not base["description"] and not base["code"]:
        return None
    return ChargeRow(
        **base,
        payer_name=clean_str(_cell(row, idx, "payer_name")),
        plan_name=clean_str(_cell(row, idx, "plan_name")),
        negotiated_dollar=to_decimal(_cell(row, idx, "standard_charge|negotiated_dollar")),
        negotiated_percentage=to_decimal(_cell(row, idx, "standard_charge|negotiated_percentage")),
        negotiated_algorithm=clean_str(_cell(row, idx, "standard_charge|negotiated_algorithm")),
        methodology=clean_str(_cell(row, idx, "standard_charge|methodology")),
        median_amount=to_decimal(_cell(row, idx, "median_amount")),
        percentile_10=to_decimal(_cell(row, idx, "10th_percentile")),
        percentile_90=to_decimal(_cell(row, idx, "90th_percentile")),
        count=to_int(_cell(row, idx, "count")),
        additional_notes=clean_str(_cell(row, idx, "additional_generic_notes")),
    )


def _wide_rows(
    row: Sequence[str],
    idx: dict[str, int],
    payer_groups: dict[tuple[str, str], dict[str, int]],
) -> Iterator[ChargeRow]:
    base = _item_fields(row, idx)
    if not base["description"] and not base["code"]:
        return
    generic_notes = clean_str(_cell(row, idx, "additional_generic_notes"))

    emitted = False
    for (payer, plan), cols in payer_groups.items():
        def g(fieldname: str):
            i = cols.get(fieldname)
            return row[i] if i is not None and i < len(row) else None

        neg_dollar = to_decimal(g("negotiated_dollar"))
        neg_pct = to_decimal(g("negotiated_percentage"))
        neg_algo = clean_str(g("negotiated_algorithm"))
        if neg_dollar is None and neg_pct is None and neg_algo is None:
            continue  # this payer has no rate for this item
        emitted = True
        yield ChargeRow(
            **base,
            payer_name=payer,
            plan_name=plan,
            negotiated_dollar=neg_dollar,
            negotiated_percentage=neg_pct,
            negotiated_algorithm=neg_algo,
            methodology=clean_str(g("methodology")),
            median_amount=to_decimal(g("median_amount")),
            percentile_10=to_decimal(g("10th_percentile")),
            percentile_90=to_decimal(g("90th_percentile")),
            count=to_int(g("count")),
            additional_notes=clean_str(g("additional_payer_notes")) or generic_notes,
        )

    if not emitted:
        # Item has gross/cash but no negotiated payer rates: keep one baseline row.
        yield ChargeRow(
            **base,
            payer_name=None,
            plan_name=None,
            negotiated_dollar=None,
            negotiated_percentage=None,
            negotiated_algorithm=None,
            methodology=None,
            median_amount=None,
            percentile_10=None,
            percentile_90=None,
            count=None,
            additional_notes=generic_notes,
        )
