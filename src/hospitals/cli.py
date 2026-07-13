"""Command-line interface for the hospital knowledge base.

Examples
--------
Ingest active Kansas hospitals from the latest CMS POS file into SQLite::

    hospitals ingest --state KS

Load into PostgreSQL instead::

    hospitals ingest --state MD \
        --database-url postgresql+psycopg://user:pass@localhost/hospitals

Run offline against a downloaded POS CSV::

    hospitals ingest --state KS --input-file data/pos.csv
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .cms_pos import CmsUnavailableError
from .db import count_charges, count_hospitals, make_engine
from .ingest import ingest_state
from .ingest_charges import ingest_charge_path
from .link import link_charges, load_crosswalk
from .logging_config import configure_logging, get_logger

log = get_logger(__name__)

DEFAULT_DB_URL = "sqlite:///data/hospitals.sqlite"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hospitals",
        description="Ingest CMS Provider of Services data into a hospital knowledge base.",
    )
    parser.add_argument("--version", action="version", version=f"hospitals {__version__}")
    parser.add_argument(
        "--log-level",
        default=None,
        help="Logging level (DEBUG, INFO, WARNING, ...). Defaults to INFO / $HOSPITALS_LOG_LEVEL.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Download and load hospitals for a state.")
    ingest.add_argument(
        "--state",
        default="KS",
        help="State USPS code or name to ingest (default: KS).",
    )
    ingest.add_argument(
        "--database-url",
        default=DEFAULT_DB_URL,
        help=f"SQLAlchemy database URL (default: {DEFAULT_DB_URL}).",
    )
    ingest.add_argument(
        "--input-file",
        default=None,
        help="Path to a local POS CSV instead of downloading from CMS (offline path).",
    )
    ingest.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include providers whose termination code is not active.",
    )
    ingest.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on raw records read (smoke testing).",
    )
    ingest.add_argument(
        "--echo-sql",
        action="store_true",
        help="Echo SQL statements (debugging).",
    )

    stats = sub.add_parser("stats", help="Show row counts in the database.")
    stats.add_argument("--database-url", default=DEFAULT_DB_URL)
    stats.add_argument("--state", default=None, help="Optional state filter.")

    charges = sub.add_parser(
        "ingest-charges",
        help="Load a CMS price transparency (standard charges) file or directory.",
    )
    charges.add_argument(
        "path",
        help="Path to an MRF .csv/.zip, or a directory of them.",
    )
    charges.add_argument("--database-url", default=DEFAULT_DB_URL)
    charges.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on items (data rows) read per file (smoke testing large files).",
    )
    charges.add_argument("--echo-sql", action="store_true")

    link = sub.add_parser(
        "link-charges",
        help="Link price-transparency files to POS hospitals (fills charge_sources.ccn).",
    )
    link.add_argument("--database-url", default=DEFAULT_DB_URL)
    link.add_argument(
        "--crosswalk",
        default=None,
        help="Optional NPI->CCN crosswalk CSV to load before linking.",
    )
    link.add_argument(
        "--no-name-fallback",
        action="store_true",
        help="Disable the name+state heuristic; use the crosswalk only.",
    )

    xwalk = sub.add_parser(
        "load-crosswalk",
        help="Load an NPI->CCN crosswalk CSV into the database.",
    )
    xwalk.add_argument("path", help="CSV with 'npi' and 'ccn' columns.")
    xwalk.add_argument("--database-url", default=DEFAULT_DB_URL)
    xwalk.add_argument("--source", default="manual", help="Provenance label.")

    return parser


def _cmd_ingest(args: argparse.Namespace) -> int:
    try:
        summary = ingest_state(
            state=args.state,
            database_url=args.database_url,
            input_file=args.input_file,
            active_only=not args.include_inactive,
            limit=args.limit,
            echo_sql=args.echo_sql,
        )
    except CmsUnavailableError as exc:
        log.error("Ingestion failed — CMS unavailable: %s", exc)
        print(
            "\nERROR: could not reach CMS. If you are running in a restricted "
            "network, download the POS CSV manually and pass --input-file.",
            file=sys.stderr,
        )
        return 2
    except KeyError as exc:
        log.error("Unknown state: %s", exc)
        return 2

    print(
        f"\n{summary.state}: read {summary.records_read} rows, "
        f"{summary.hospitals_matched} hospitals matched "
        f"({summary.active_matched} active), loaded {summary.loaded}."
    )
    if summary.edition:
        print(f"Source edition: {summary.edition}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    total = count_hospitals(engine, args.state)
    charges = count_charges(engine)
    label = f" in {args.state.upper()}" if args.state else ""
    print(f"{total} hospitals{label}.")
    print(f"{charges} standard-charge rows.")
    return 0


def _cmd_ingest_charges(args: argparse.Namespace) -> int:
    try:
        summaries = ingest_charge_path(
            args.path,
            database_url=args.database_url,
            limit=args.limit,
            echo_sql=args.echo_sql,
        )
    except FileNotFoundError as exc:
        log.error("File not found: %s", exc)
        print(f"\nERROR: no such file or directory: {exc}", file=sys.stderr)
        return 2

    total = sum(s.charges_loaded for s in summaries)
    print()
    for s in summaries:
        print(
            f"{s.hospital_name or s.source_file} "
            f"[EIN {s.ein}, NPI {s.primary_npi}, {s.layout}]: "
            f"loaded {s.charges_loaded} charge rows."
        )
    if len(summaries) > 1:
        print(f"\nTotal: {total} charge rows from {len(summaries)} files.")
    return 0


def _cmd_link_charges(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    if args.crosswalk:
        try:
            loaded = load_crosswalk(engine, args.crosswalk)
        except (FileNotFoundError, ValueError) as exc:
            log.error("Could not load crosswalk: %s", exc)
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Loaded {loaded} crosswalk rows.")
    summary = link_charges(engine, use_name_fallback=not args.no_name_fallback)
    print(
        f"\nLinked {summary.by_crosswalk + summary.by_name}/{summary.total} "
        f"charge sources (crosswalk={summary.by_crosswalk}, "
        f"name+state={summary.by_name}, unlinked={summary.unlinked})."
    )
    return 0


def _cmd_load_crosswalk(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    try:
        loaded = load_crosswalk(engine, args.path, source=args.source)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Could not load crosswalk: %s", exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Loaded {loaded} NPI->CCN crosswalk rows.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "stats":
        return _cmd_stats(args)
    if args.command == "ingest-charges":
        return _cmd_ingest_charges(args)
    if args.command == "link-charges":
        return _cmd_link_charges(args)
    if args.command == "load-crosswalk":
        return _cmd_load_crosswalk(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
