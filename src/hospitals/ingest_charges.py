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
from .db import init_db, load_charges, make_engine
from .logging_config import get_logger

log = get_logger(__name__)


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

    engine = engine or make_engine(database_url, echo=echo_sql)
    init_db(engine)

    log.info("Ingesting price transparency file: %s", path)
    metadata_obj, charges = pt.read_mrf(path, limit=limit)
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
) -> list[ChargeIngestSummary]:
    """Ingest a single file or every ``.csv``/``.zip`` in a directory."""

    if os.path.isdir(path):
        files = sorted(
            f
            for f in glob.glob(os.path.join(path, "*"))
            if f.lower().endswith((".csv", ".zip"))
        )
        if not files:
            raise FileNotFoundError(f"no .csv/.zip files in directory: {path}")
    else:
        files = [path]

    # Share one engine across all files in the batch.
    engine = make_engine(database_url, echo=echo_sql)
    init_db(engine)

    summaries: list[ChargeIngestSummary] = []
    for f in files:
        summaries.append(
            ingest_charge_file(
                f,
                database_url=database_url,
                limit=limit,
                echo_sql=echo_sql,
                engine=engine,
            )
        )
    return summaries
