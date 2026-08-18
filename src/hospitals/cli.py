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

from sqlalchemy.exc import OperationalError

from . import __version__
from .cms_pos import CmsUnavailableError
from .archive import archive_ingested
from .db import count_charges, count_hospitals, make_engine
from .duplicates import find_duplicate_loads, prune_redownloads
from .gap import build_gap_report, write_xlsx
from .ingest import ingest_state
from .ingest_charges import ingest_charge_path
from .link import link_charges, load_crosswalk
from .logging_config import configure_logging, get_logger
from .repair import repair_eins

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
        help="State USPS code or name to ingest, or ALL for every state "
        "in one pass (default: KS).",
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
    ingest.add_argument(
        "--dataset-title",
        default=None,
        help="CMS dataset to look for. CMS publishes several 'Provider of "
        "Services' files for different provider systems; name one to choose it.",
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
    charges.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files already loaded (makes a large batch resumable).",
    )
    charges.add_argument(
        "--recursive",
        action="store_true",
        help="Descend into subdirectories (a system's facilities often arrive in one).",
    )
    charges.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep going when a file fails; report the failures at the end.",
    )

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

    dupes = sub.add_parser(
        "duplicates",
        help="Report charge files that hold the same rows more than once.",
    )
    dupes.add_argument("--database-url", default=DEFAULT_DB_URL)
    dupes.add_argument(
        "--limit", type=int, default=25, help="Groups to list (default: 25)."
    )

    prune = sub.add_parser(
        "prune-duplicates",
        help="Delete extra copies of files downloaded twice (dry run unless --apply).",
    )
    prune.add_argument("--database-url", default=DEFAULT_DB_URL)
    prune.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it, only report what would go.",
    )
    prune.add_argument(
        "--limit", type=int, default=40, help="Files to list (default: 40)."
    )

    repair = sub.add_parser(
        "repair-eins",
        help="Rewrite stored EINs to match their filenames (dry run unless --apply).",
    )
    repair.add_argument("--database-url", default=DEFAULT_DB_URL)
    repair.add_argument(
        "--apply",
        action="store_true",
        help="Actually rewrite. Without it, only report what disagrees.",
    )
    repair.add_argument(
        "--sources-only",
        action="store_true",
        help="Fix charge_sources only; leave the charge rows for a later pass.",
    )
    repair.add_argument(
        "--limit", type=int, default=40, help="Sources to list (default: 40)."
    )

    archive = sub.add_parser(
        "archive-ingested",
        help="Move files already loaded out of a folder (dry run unless --apply).",
    )
    archive.add_argument("path", help="Folder of MRF files to tidy.")
    archive.add_argument(
        "--to",
        required=True,
        dest="destination",
        help="Folder to move loaded files into (created if missing).",
    )
    archive.add_argument("--database-url", default=DEFAULT_DB_URL)
    archive.add_argument(
        "--apply",
        action="store_true",
        help="Actually move the files. Without it, only report what would move.",
    )
    archive.add_argument(
        "--recursive",
        action="store_true",
        help="Descend into subdirectories, mirroring their layout at the destination.",
    )

    gap = sub.add_parser(
        "gap-report",
        help="List hospitals with no price-transparency file yet (writes .xlsx).",
    )
    gap.add_argument("--database-url", default=DEFAULT_DB_URL)
    gap.add_argument(
        "--output",
        default="hospitals_to_download.xlsx",
        help="Workbook to write (default: hospitals_to_download.xlsx).",
    )
    gap.add_argument(
        "--min-system-size",
        type=int,
        default=2,
        help="Hospitals a candidate system needs before it is ranked as one "
        "rather than listed as independents (default: 2).",
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
            dataset_title=args.dataset_title,
        )
    except CmsUnavailableError as exc:
        log.error("Ingestion failed — CMS unavailable: %s", exc)
        print(
            "\nERROR: could not reach CMS. If you are running in a restricted "
            "network, download the POS CSV manually and pass --input-file.",
            file=sys.stderr,
        )
        return 2
    except LookupError as exc:
        # CMS renamed or withdrew the dataset. The catalogs answered; the
        # dataset was not in them. Printing the message beats a traceback,
        # because the message names the titles CMS is actually publishing.
        if isinstance(exc, KeyError):
            log.error("Unknown state: %s", exc)
            return 2
        log.error("Ingestion failed — dataset not found: %s", exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(
            "\nIf CMS has renamed the dataset, download the POS CSV from "
            "data.cms.gov and pass --input-file.",
            file=sys.stderr,
        )
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
            skip_existing=args.skip_existing,
            continue_on_error=args.continue_on_error,
            recursive=args.recursive,
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


def _cmd_duplicates(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    report = find_duplicate_loads(engine)

    if not report.groups:
        print("\nNo duplicate loads found.")
        return 0

    print(
        f"\n{report.redundant_files} of {report.total_sources} charge file(s) hold rows "
        f"already stored under the same EIN."
    )
    print(
        f"{report.redundant_rows:,} redundant rows — {report.share_of_rows}% of "
        f"{report.total_rows:,} total."
    )

    print(
        f"  {report.redownload_rows:,} ({report.redownload_share}%) are the same file "
        f"downloaded twice — {len(report.redownload_files)} file(s), safe to delete."
    )
    print(
        f"  {report.per_facility_rows:,} ({report.per_facility_share}%) are one dataset "
        "published per facility — a schema change, not a deletion."
    )

    print(f"\nLargest {min(args.limit, len(report.groups))} group(s):")
    for group in report.groups[: args.limit]:
        kind = (
            f"{group.per_facility_datasets} facilities on one dataset"
            if group.looks_like_one_file_per_facility
            else "same file downloaded more than once"
        )
        print(
            f"\n  {group.entity} — {group.copies} copies x {group.charge_count:,} rows "
            f"({group.redundant_rows:,} redundant; {kind})"
        )
        extras = set(group.redownload_files)
        for name in group.source_files[:6]:
            mark = "  [re-download]" if name in extras else ""
            print(f"    {name}{mark}")
        if group.copies > 6:
            print(f"    ... and {group.copies - 6} more")

    print(
        "\nNothing was deleted. Run 'hospitals prune-duplicates' to drop the "
        "re-downloads; the per-facility copies stay, because a charge_sources "
        "row is also the record that a facility exists and what it is called."
    )
    return 0


def _cmd_prune_duplicates(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    summary = prune_redownloads(engine, apply=args.apply)

    if not summary.file_count:
        print("\nNo re-downloaded copies found.")
        return 0

    verb = "Deleted" if summary.applied else "Would delete"
    print(f"\n{verb} {summary.file_count} re-downloaded file(s), {summary.rows:,} rows.")
    for name in summary.files[: args.limit]:
        print(f"  {name}")
    if summary.file_count > args.limit:
        print(f"  ... and {summary.file_count - args.limit} more")

    if summary.applied:
        print(
            "\nSQLite frees these pages for reuse but does not shrink the file. "
            "The next load fills the space instead of growing the database; "
            "VACUUM would return it to the volume but needs as much free disk "
            "as the database is large."
        )
    else:
        print("\nDry run — nothing was deleted. Re-run with --apply.")
    return 0


def _cmd_repair_eins(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    summary = repair_eins(
        engine, apply=args.apply, sources_only=args.sources_only
    )

    if not summary.source_count:
        print("\nEvery stored EIN matches its filename.")
        return 0

    verb = "Repaired" if summary.applied else "Would repair"
    print(
        f"\n{verb} {summary.source_count} source(s) whose stored EIN disagrees "
        f"with the filename, covering {summary.charge_rows_affected:,} charge row(s)."
    )
    for fix in summary.fixes[: args.limit]:
        note = "  <- invented from the NPI" if fix.drops_an_invented_ein else ""
        print(
            f"  {fix.stored_ein} -> {fix.correct_ein}  "
            f"({fix.charge_count:,} rows)  {fix.source_file}{note}"
        )
    if summary.source_count > args.limit:
        print(f"  ... and {summary.source_count - args.limit} more")

    if summary.applied:
        if summary.sources_only:
            print(
                "\ncharge_sources is correct; standard_charges.ein still holds the "
                "old value. Re-run without --sources-only to finish."
            )
        else:
            print(f"\nRewrote {summary.charge_rows_rewritten:,} charge row(s).")
    else:
        print(
            "\nDry run — nothing was written. Re-run with --apply, or with "
            "--apply --sources-only for an instant pass that leaves the charge "
            "rows for later."
        )
    return 0


def _cmd_archive_ingested(args: argparse.Namespace) -> int:
    try:
        summary = archive_ingested(
            args.path,
            args.destination,
            database_url=args.database_url,
            apply=args.apply,
            recursive=args.recursive,
        )
    except NotADirectoryError as exc:
        log.error("Not a directory: %s", exc)
        print(f"\nERROR: not a directory: {exc}", file=sys.stderr)
        return 2

    verb = "Would move" if summary.dry_run else "Moved"
    print(f"\n{verb} {len(summary.moved)} loaded file(s) to {args.destination}.")

    if summary.not_loaded:
        print(f"\nStaying put — not loaded ({len(summary.not_loaded)}):")
        for name in summary.not_loaded:
            print(f"  {name}")
    if summary.collisions:
        print(f"\nStaying put — name already at destination ({len(summary.collisions)}):")
        for name in summary.collisions:
            print(f"  {name}")
    if summary.other_files:
        print(f"\nIgnored (not an ingestible file type): {len(summary.other_files)}")
    if summary.skipped_dirs:
        print(
            f"\nNot looked at — subdirector"
            f"{'y' if len(summary.skipped_dirs) == 1 else 'ies'} "
            f"({len(summary.skipped_dirs)}), pass --recursive to include:"
        )
        for name in summary.skipped_dirs:
            print(f"  {name}/")

    if summary.dry_run and summary.moved:
        print("\nThis was a dry run. Re-run with --apply to move them.")
    return 0


def _cmd_gap_report(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    report = build_gap_report(engine, min_system_size=args.min_system_size)

    if not report.total_hospitals:
        print(
            "\nNo hospitals in the database — load the POS universe first:\n"
            "  hospitals ingest --state ALL --database-url <url>",
            file=sys.stderr,
        )
        return 2

    write_xlsx(report, args.output)

    print(
        f"\n{report.total_covered}/{report.total_hospitals} hospitals have a "
        f"price-transparency file ({report.total_remaining} to go)."
    )
    print(
        f"{len(report.systems)} candidate systems, "
        f"{len(report.independents)} independents."
    )
    known = [s for s in report.systems if s.already_publishing]
    if known:
        print(f"{len(known)} of those systems already publish a file we have parsed.")
    if report.unattributed:
        rows = sum(u.charge_count for u in report.unattributed)
        print(
            f"{len(report.unattributed)} file(s) held but not attributed to any "
            f"hospital ({rows} charge rows) — see the Unattributed Files sheet."
        )
    never = report.uncrawled_states
    if never:
        print(f"States with nothing ingested yet ({len(never)}): {', '.join(never)}")
    print(f"\nWrote {args.output}")
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

    try:
        return _dispatch(parser, args)
    except OperationalError as exc:
        if "database is locked" not in str(exc).lower():
            raise
        # A traceback here says nothing the first line does not, and buries it.
        log.error("Database is locked by another writer")
        print(
            "\nERROR: the database is locked by another writer — an ingest is "
            "almost certainly still running.\n"
            "Nothing was written; this command is safe to re-run once it "
            "finishes.\n"
            "Check with:  ps aux | grep ingest-charges",
            file=sys.stderr,
        )
        return 3


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "stats":
        return _cmd_stats(args)
    if args.command == "ingest-charges":
        return _cmd_ingest_charges(args)
    if args.command == "link-charges":
        return _cmd_link_charges(args)
    if args.command == "duplicates":
        return _cmd_duplicates(args)
    if args.command == "prune-duplicates":
        return _cmd_prune_duplicates(args)
    if args.command == "repair-eins":
        return _cmd_repair_eins(args)
    if args.command == "archive-ingested":
        return _cmd_archive_ingested(args)
    if args.command == "gap-report":
        return _cmd_gap_report(args)
    if args.command == "load-crosswalk":
        return _cmd_load_crosswalk(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
