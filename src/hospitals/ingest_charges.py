"""Ingest CMS Hospital Price Transparency files into the knowledge base.

Unlike the Provider of Services file, there is no single national feed of
machine-readable "standard charges" files — each hospital publishes its own on
its website. So ingestion is file-driven: point it at a local ``.csv``/``.zip``
(or a directory of them) that you have downloaded.

Each file is parsed (tall or wide layout), keyed by the hospital's EIN and
organizational NPI, and loaded with its full set of negotiated rates. The
NPI/EIN are the eventual join keys back to the Provider of Services hospitals
(via an NPI<->CCN crosswalk, a later step).
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass

from . import price_transparency as pt
from .db import init_db, load_charges, loaded_source_files, make_engine
from .logging_config import get_logger

log = get_logger(__name__)

# Extensions the ingester understands. ``.gz`` covers gzipped CSV and JSON,
# which large systems use for multi-gigabyte exports; they are decompressed on
# the fly rather than unpacked to disk.
SUPPORTED = (".csv", ".zip", ".json", ".xlsx", ".xlsm", ".gz")


@dataclass
class ChargeIngestSummary:
    source_file: str
    hospital_name: str | None
    ein: str | None
    primary_npi: str | None
    layout: str | None
    charges_loaded: int


def ingest_charge_file(
    path: str,
    *,
    database_url: str = "sqlite:///data/hospitals.sqlite",
    limit: int | None = None,
    echo_sql: bool = False,
    engine=None,
) -> ChargeIngestSummary:
    """Ingest a single MRF file into the database."""

    if not os.path.exists(path):
        raise FileNotFoundError(path)

    # A caller running a batch has already created the schema; re-checking it
    # per file adds a log line for every one of several hundred files.
    if engine is None:
        engine = make_engine(database_url, echo=echo_sql)
        init_db(engine)

    log.info("Ingesting price transparency file: %s", path)
    metadata_obj, charges = pt.read_any(path, limit=limit)
    loaded = load_charges(engine, metadata_obj, charges)

    return ChargeIngestSummary(
        source_file=metadata_obj.source_file or os.path.basename(path),
        hospital_name=metadata_obj.hospital_name,
        ein=metadata_obj.ein,
        primary_npi=metadata_obj.primary_npi,
        layout=metadata_obj.layout,
        charges_loaded=loaded,
    )


def ingest_charge_path(
    path: str,
    *,
    database_url: str = "sqlite:///data/hospitals.sqlite",
    limit: int | None = None,
    echo_sql: bool = False,
    skip_existing: bool = False,
    continue_on_error: bool = False,
) -> list[ChargeIngestSummary]:
    """Ingest a single file, or every supported file in a directory.

    A real download folder is a batch of hundreds of files, so two options
    matter at that scale: ``skip_existing`` makes the run resumable (files
    already loaded are left alone), and ``continue_on_error`` keeps one
    malformed file from ending the batch.
    """

    # Check the target up front: a mistyped folder is not a per-file failure,
    # and letting it through would have ``--continue-on-error`` report it as
    # one bad file rather than "that path does not exist".
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    ignored: list[str] = []
    if os.path.isdir(path):
        files = []
        for entry in sorted(glob.glob(os.path.join(path, "*"))):
            if os.path.isdir(entry):
                continue
            if entry.lower().endswith(SUPPORTED):
                files.append(entry)
            else:
                ignored.append(os.path.basename(entry))
        if not files:
            raise FileNotFoundError(
                f"no {'/'.join(SUPPORTED)} files in directory: {path}"
            )
    else:
        files = [path]

    # Share one engine across all files in the batch.
    engine = make_engine(database_url, echo=echo_sql)
    init_db(engine)

    done: set[str] = set()
    if skip_existing:
        done = loaded_source_files(engine)

    # An unrecognized extension used to be dropped without a word, which is how
    # a half-finished .crdownload cost us a hospital in an earlier batch. Say
    # what is being left behind before the run starts.
    if ignored:
        log.warning("Ignoring %d file(s) of unsupported type:", len(ignored))
        for name in ignored:
            log.warning("  %s", name)

    total = len(files)
    summaries: list[ChargeIngestSummary] = []
    skipped = 0
    failed: list[tuple[str, str]] = []

    for n, f in enumerate(files, start=1):
        name = pt._strip_hash_prefix(f)
        if skip_existing and name in done:
            skipped += 1
            log.info("[%d/%d] skip (already loaded): %s", n, total, name)
            continue
        log.info("[%d/%d] %s", n, total, name)
        try:
            summaries.append(
                ingest_charge_file(
                    f,
                    database_url=database_url,
                    limit=limit,
                    echo_sql=echo_sql,
                    engine=engine,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not end a batch
            if not continue_on_error:
                raise
            failed.append((name, str(exc)))
            log.error("[%d/%d] FAILED %s: %s", n, total, name, exc)

    if skipped:
        log.info("Skipped %d file(s) already loaded", skipped)
    if failed:
        log.warning("%d file(s) failed:", len(failed))
        for name, err in failed:
            log.warning("  %s: %s", name, err)
    if ignored:
        log.warning(
            "%d file(s) were not an ingestible type and were never read", len(ignored)
        )
    return summaries
