"""Merge a second SQLite vault's price-transparency data into the main one.

Two "vaults" (SQLite files built by this same schema) sometimes exist side by
side — a full production database and a smaller one used to trial a batch of
newly downloaded MRF files before folding them in. This module folds the
second file's *new* charge data into the first, using ``charge_sources.source_file``
as the natural key: a file already present in the target (by filename, which
the schema already declares ``unique``) is never re-copied, no matter how
many rows it holds in either copy.

It only ever writes to the TARGET database, and only after copying it to a
timestamped backup first. Given ``apply=False`` (the default everywhere this
is called) it changes nothing and only reports what it would do — the same
dry-run-unless-told posture as ``apply-links``.

Only the tables that make up "did we already have this file's prices" are
merged: ``charge_sources`` and ``standard_charges``. The ``hospitals`` table
is intentionally left alone — the target's hospital registry is assumed
authoritative, since a probe/staging database commonly has an incomplete or
empty one, and a source database with zero hospital rows cannot have linked
anything no matter how many files it parsed. Run ``link-charges`` (or
``suggest-links`` / ``apply-links``) against the target after merging to
attribute the newly arrived files.

This only supports SQLite-to-SQLite merges; there is no cross-dialect path
here (Postgres users would restore a dump into a scratch SQLite file first).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
from dataclasses import dataclass, field

from .logging_config import get_logger

log = get_logger(__name__)

_SOURCE_COLUMNS = [
    "source_file", "hospital_name", "location_name", "hospital_address",
    "ein", "primary_npi", "npis", "license_number", "license_state",
    "mrf_version", "layout", "last_updated_on", "ccn", "link_method",
    "charge_count", "ingested_at",
]

_CHARGE_COLUMNS = [
    "ein", "primary_npi", "description", "code", "code_type",
    "additional_codes", "billing_class", "setting",
    "drug_unit_of_measurement", "drug_type_of_measurement", "modifiers",
    "gross_charge", "discounted_cash", "min_charge", "max_charge",
    "payer_name", "plan_name", "negotiated_dollar", "negotiated_percentage",
    "negotiated_algorithm", "methodology", "median_amount", "percentile_10",
    "percentile_90", "count", "additional_notes",
]


@dataclass
class FileDecision:
    source_file: str
    hospital_name: str | None
    license_state: str | None
    charge_count: int | None
    action: str  # "add" | "skip_duplicate"
    reason: str = ""


@dataclass
class MergeSummary:
    target_path: str
    source_path: str
    decisions: list[FileDecision] = field(default_factory=list)
    charge_rows_added: int = 0
    backup_path: str | None = None
    applied: bool = False

    @property
    def to_add(self) -> list[FileDecision]:
        return [d for d in self.decisions if d.action == "add"]

    @property
    def duplicates(self) -> list[FileDecision]:
        return [d for d in self.decisions if d.action == "skip_duplicate"]


def sqlite_path(database_url: str) -> str:
    """Extract a filesystem path from a ``sqlite:///`` or ``sqlite:////`` URL."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(
            f"merge-vaults only supports SQLite databases; got {database_url!r}"
        )
    return database_url[len(prefix):]


def plan_merge(target_path: str, source_path: str) -> MergeSummary:
    """Decide, per source file, whether it is new or already present.

    Read-only: attaches the source file to a throwaway connection against the
    target and never writes. Safe to call at any time, on the live database.
    """

    summary = MergeSummary(target_path=target_path, source_path=source_path)
    conn = sqlite3.connect(target_path)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (source_path,))
        rows = conn.execute(
            """
            SELECT s.source_file, s.hospital_name, s.license_state, s.charge_count,
                   t.source_file IS NOT NULL AS already_present
            FROM src.charge_sources s
            LEFT JOIN main.charge_sources t ON t.source_file = s.source_file
            ORDER BY s.charge_count DESC
            """
        ).fetchall()
        for source_file, hospital_name, license_state, charge_count, already_present in rows:
            if already_present:
                summary.decisions.append(
                    FileDecision(
                        source_file, hospital_name, license_state, charge_count,
                        "skip_duplicate", "source_file already in target",
                    )
                )
            else:
                summary.decisions.append(
                    FileDecision(source_file, hospital_name, license_state, charge_count, "add")
                )
    finally:
        conn.execute("DETACH DATABASE src")
        conn.close()
    return summary


def apply_merge(target_path: str, source_path: str, backup_dir: str | None = None) -> MergeSummary:
    """Copy every new file's ``charge_sources`` + ``standard_charges`` rows into target.

    Always backs up the target first, and runs the whole copy as one
    transaction: either every new file's rows land, or (on error) none do and
    the target is byte-identical to before the call.
    """

    summary = plan_merge(target_path, source_path)
    to_add = summary.to_add
    if not to_add:
        summary.applied = True
        return summary

    backup_dir = backup_dir or os.path.dirname(target_path) or "."
    os.makedirs(backup_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = os.path.join(
        backup_dir, f"{os.path.basename(target_path)}.pre-merge-{stamp}.bak"
    )
    shutil.copy2(target_path, backup_path)
    summary.backup_path = backup_path
    log.info("Backed up %s -> %s before merge", target_path, backup_path)

    src_col_list = ", ".join(_SOURCE_COLUMNS)
    src_placeholders = ", ".join("?" for _ in _SOURCE_COLUMNS)
    chg_col_list = ", ".join(_CHARGE_COLUMNS)

    conn = sqlite3.connect(target_path)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (source_path,))
        conn.execute("BEGIN")
        rows_added = 0
        for decision in to_add:
            src_row = conn.execute(
                f"SELECT id, {src_col_list} FROM src.charge_sources WHERE source_file = ?",
                (decision.source_file,),
            ).fetchone()
            old_source_id, values = src_row[0], src_row[1:]
            cur = conn.execute(
                f"INSERT INTO main.charge_sources ({src_col_list}) VALUES ({src_placeholders})",
                values,
            )
            new_source_id = cur.lastrowid
            conn.execute(
                f"""
                INSERT INTO main.standard_charges (source_id, {chg_col_list})
                SELECT ?, {chg_col_list} FROM src.standard_charges WHERE source_id = ?
                """,
                (new_source_id, old_source_id),
            )
            rows_added += conn.execute("SELECT changes()").fetchone()[0]
        conn.execute("COMMIT")
        summary.charge_rows_added = rows_added
        summary.applied = True
        log.info(
            "Merged %d new files (%d charge rows) from %s into %s",
            len(to_add), rows_added, source_path, target_path,
        )
    except Exception:
        conn.execute("ROLLBACK")
        log.error("Merge failed; target rolled back, unchanged.")
        raise
    finally:
        conn.execute("DETACH DATABASE src")
        conn.close()
    return summary
