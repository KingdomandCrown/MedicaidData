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
import csv
import os
import sys

from sqlalchemy.exc import OperationalError

from . import __version__
from .cms_pos import CmsUnavailableError
from .archive import archive_ingested
from .coverage import coverage_for
from .crosswalk import fetch_crosswalk
from .db import (
    EmptyDatabase,
    count_charges,
    count_hospitals,
    make_engine,
    require_schema,
)
from .duplicates import find_duplicate_loads, prune_redownloads
from .gap import build_gap_report, write_xlsx
from .mrf_discovery import MANIFEST_COLUMNS, discover_one, to_row
from .mrf_fetch import MAX_BYTES, Fetched, fetch_one, requests_opener
from .mrf_targets import DEFAULT_INFO_PATH, choose_targets, load_websites
from .ingest import ingest_state
from .ingest_charges import ingest_charge_path
from .link import link_charges, load_crosswalk
from .logging_config import configure_logging, get_logger
from .price import price_for_code
from .repair import backfill_npis, repair_eins
from .scan import human_bytes, scan_for_ingested, write_listing

log = get_logger(__name__)

#: The database every command reads unless told otherwise.
#:
#: Reads $HOSPITALS_DATABASE_URL first so the real database can be named once,
#: in a shell profile, instead of pasted onto every command. A shell variable
#: that has to be re-exported in each new terminal is one an hour of work can
#: silently run without.
DEFAULT_DB_URL = (
    os.environ.get("HOSPITALS_DATABASE_URL", "").strip()
    or "sqlite:///data/hospitals.sqlite"
)


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
    charges.add_argument(
        "--allow-empty",
        action="store_true",
        help="Load into a database with no hospitals in it. Refused by default: "
        "those rows can never be linked, and it is almost always a wrong "
        "--database-url.",
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

    npis = sub.add_parser(
        "backfill-npis",
        help="Store the filename's NPI on sources that carry none (dry run unless --apply).",
    )
    npis.add_argument("--database-url", default=DEFAULT_DB_URL)
    npis.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it, only report what is missing.",
    )
    npis.add_argument(
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

    price = sub.add_parser(
        "price",
        help="What one billing code costs across every hospital in the database.",
    )
    price.add_argument("code", help="A billing code, e.g. 85025 (CBC with differential).")
    price.add_argument("--database-url", default=DEFAULT_DB_URL)
    price.add_argument("--state", default=None, help="Restrict to hospitals in one state.")
    price.add_argument("--payer", default=None, help="Restrict to payers matching this text.")
    price.add_argument(
        "--include-unlinked",
        action="store_true",
        help="Include files not attributed to a hospital (wider, less certain).",
    )
    price.add_argument(
        "--limit", type=int, default=15, help="Payers to list (default: 15)."
    )

    cov = sub.add_parser(
        "coverage",
        help="Show what the database holds for one hospital or system by name.",
    )
    cov.add_argument("pattern", help="Part of a hospital, system, or file name.")
    cov.add_argument("--database-url", default=DEFAULT_DB_URL)
    cov.add_argument("--state", default=None, help="Restrict hospitals to this state.")
    cov.add_argument(
        "--limit", type=int, default=25, help="Rows to list per section (default: 25)."
    )

    scan = sub.add_parser(
        "scan-ingested",
        help="Find MRF files anywhere on disk that the database already holds.",
    )
    scan.add_argument(
        "paths",
        nargs="*",
        default=["~/Desktop", "~/Downloads", "~/Documents"],
        help="Folders to walk (default: ~/Desktop ~/Downloads ~/Documents).",
    )
    scan.add_argument("--database-url", default=DEFAULT_DB_URL)
    scan.add_argument(
        "--output",
        default=None,
        help="Write the already-ingested paths to this file, one per line.",
    )
    scan.add_argument(
        "--limit", type=int, default=20, help="Folders to list (default: 20)."
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

    disc = sub.add_parser(
        "discover-mrf",
        help="Find each hospital's machine-readable file from its own website "
        "(writes a manifest CSV; downloads nothing).",
    )
    disc.add_argument("--database-url", default=DEFAULT_DB_URL)
    disc.add_argument(
        "--state",
        action="append",
        default=None,
        help="Restrict to a state; repeatable. Omit for every state.",
    )
    disc.add_argument(
        "--websites",
        default=DEFAULT_INFO_PATH,
        help=f"hospital-info.json to read websites from (default: {DEFAULT_INFO_PATH}).",
    )
    disc.add_argument("--output", default="mrf_manifest.csv", help="Manifest to write.")
    disc.add_argument("--limit", type=int, default=None, help="Stop after N hospitals.")
    disc.add_argument(
        "--include-covered",
        action="store_true",
        help="Also ask hospitals that already have a file (to refresh a stale one).",
    )
    disc.add_argument(
        "--timeout", type=int, default=20, help="Seconds per request (default: 20)."
    )

    fetchm = sub.add_parser(
        "fetch-mrf",
        help="Download the files a discovery manifest found.",
    )
    fetchm.add_argument("manifest", help="CSV written by discover-mrf.")
    fetchm.add_argument(
        "--dest", required=True, help="Folder to download into."
    )
    fetchm.add_argument("--limit", type=int, default=None, help="Stop after N files.")
    fetchm.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="Also download rows a person has not resolved. Off by default: an "
        "ambiguous row may be a different hospital in the same system.",
    )
    fetchm.add_argument(
        "--overwrite", action="store_true", help="Re-download files already present."
    )
    fetchm.add_argument(
        "--max-bytes", type=int, default=MAX_BYTES,
        help="Stop any single download past this size.",
    )
    fetchm.add_argument(
        "--timeout", type=int, default=60, help="Seconds per request (default: 60)."
    )

    fetchx = sub.add_parser(
        "fetch-crosswalk",
        help="Download CMS Hospital Enrollments and build the NPI->CCN crosswalk.",
    )
    fetchx.add_argument("--database-url", default=DEFAULT_DB_URL)
    fetchx.add_argument(
        "--dataset-title",
        default=None,
        help="CMS dataset to read (default: Hospital Enrollments).",
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
    # Charges are only worth anything once they are attributed, and attribution
    # needs the POS universe. A database with no hospitals in it will accept
    # every row and link none of them, which is how 38 million rows and an hour
    # and a half of parsing went into the wrong file: an unset shell variable
    # fell back to the default path, and nothing objected.
    engine = make_engine(args.database_url)
    try:
        require_schema(engine, args.database_url)
        known = count_hospitals(engine)
    except EmptyDatabase:
        known = 0
    if known == 0 and not args.allow_empty:
        print(
            f"\nERROR: {args.database_url} holds no hospitals.\n"
            "  Charge files loaded here can never be linked to a CCN, because\n"
            "  there is nothing to link them to. This is almost always a wrong\n"
            "  --database-url, or $HOSPITALS_DATABASE_URL unset in this shell:\n"
            f"    HOSPITALS_DATABASE_URL is "
            + (f"set to {os.environ['HOSPITALS_DATABASE_URL']}"
               if os.environ.get("HOSPITALS_DATABASE_URL") else "NOT SET")
            + "\n"
            "  Load the hospitals first:  hospitals ingest --state ALL\n"
            "  Or pass --allow-empty if this really is a scratch database.",
            file=sys.stderr,
        )
        return 2

    print(f"\nLoading into {args.database_url} ({known:,} hospitals known).")
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
    require_schema(engine, args.database_url)
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
    require_schema(engine, args.database_url)
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
    require_schema(engine, args.database_url)
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
    require_schema(engine, args.database_url)
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


def _cmd_backfill_npis(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    require_schema(engine, args.database_url)
    summary = backfill_npis(engine, apply=args.apply)

    if not summary.source_count:
        print("\nEvery source that can have an NPI has one.")
        return 0

    verb = "Filled in" if summary.applied else "Would fill in"
    print(
        f"\n{verb} the NPI for {summary.source_count} source(s) that carry none, "
        f"covering {summary.charge_rows_covered:,} charge row(s)."
    )
    for fix in summary.fixes[: args.limit]:
        print(f"  {fix.npi}  ({fix.charge_count:,} rows)  {fix.source_file}")
    if summary.source_count > args.limit:
        print(f"  ... and {summary.source_count - args.limit} more")

    if not summary.applied:
        print("\nDry run — nothing was written. Re-run with --apply.")
    else:
        print("\nNow re-link:  hospitals link-charges --database-url <url>")
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


def _cmd_price(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    require_schema(engine, args.database_url)
    report = price_for_code(
        engine,
        args.code,
        state=args.state,
        payer=args.payer,
        linked_only=not args.include_unlinked,
    )

    if not report.rows:
        print(f"\nNo prices recorded for code {args.code}.")
        return 0

    where = f" in {args.state.upper()}" if args.state else ""
    print(
        f"\nCode {args.code}"
        + (f" — {report.common_description}" if report.common_description else "")
    )
    print(
        f"{report.rows:,} price(s) across {report.hospital_count:,} hospital(s){where}."
    )

    def line(spread):
        if not spread.count:
            return f"  {spread.label:<16}  no prices recorded"
        return (
            f"  {spread.label:<16}  median ${spread.median:,.2f}"
            f"   (25th ${spread.p25:,.2f} · 75th ${spread.p75:,.2f})"
            f"   n={spread.count:,}"
        )

    print()
    for spread in (report.gross, report.cash, report.negotiated):
        print(line(spread))

    payers = report.top_payers[: args.limit]
    if payers:
        print(f"\nNegotiated by payer (top {len(payers)} by volume):")
        for payer in payers:
            print(f"  ${payer.median:>10,.2f}   n={payer.count:>6,}   {payer.payer}")

    if report.negotiated.count:
        print(
            f"\nSpread: ${report.negotiated.low:,.2f} to ${report.negotiated.high:,.2f}. "
            "Medians, not means — a chargemaster's long tail makes an average "
            "no patient experiences."
        )
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    require_schema(engine, args.database_url)
    report = coverage_for(engine, args.pattern, state=args.state)

    print(f"\n{report.diagnosis}.")

    if report.sources:
        print(
            f"\nCharge files matching {args.pattern!r}: {len(report.sources)} "
            f"({len(report.linked_sources)} linked, {len(report.unlinked_sources)} not), "
            f"{report.charge_rows:,} rows."
        )
        for src in report.sources[: args.limit]:
            if src.is_linked:
                where = f"CCN {src.ccn} via {src.link_method}"
            elif src.shares_with:
                where = f"same data as CCN {src.shares_ccn}"
            else:
                where = "UNLINKED"
            print(f"  {src.charge_count:>12,} rows  {where:<28}  {src.source_file}")
        if len(report.sources) > args.limit:
            print(f"  ... and {len(report.sources) - args.limit} more")
        if report.shared_sources:
            print(
                f"\n  {len(report.shared_sources)} file(s) hold a dataset a linked "
                "sibling already carries — a freestanding ED or provider-based "
                "department has no CCN of its own and publishes its parent's "
                "chargemaster. Not a gap."
            )
        if report.missing_rows:
            print(
                f"\n  {report.missing_rows:,} row(s) sit in files with no CCN and no "
                "linked sibling. Run fetch-crosswalk, backfill-npis, then link-charges."
            )

    if report.hospitals:
        print(
            f"\nHospitals matching {args.pattern!r}: {len(report.hospitals)} "
            f"({len(report.covered_hospitals)} with a file)."
        )
        for hosp in report.hospitals[: args.limit]:
            mark = f"{hosp.charge_rows:,} rows" if hosp.is_covered else "no file"
            print(f"  {hosp.ccn}  {hosp.state or '--'}  {mark:>16}  {hosp.name}")
        if len(report.hospitals) > args.limit:
            print(f"  ... and {len(report.hospitals) - args.limit} more")

    return 0


def _cmd_scan_ingested(args: argparse.Namespace) -> int:
    summary = scan_for_ingested(args.paths, database_url=args.database_url)

    if summary.missing_roots:
        print(f"\nSkipped (not a directory): {', '.join(summary.missing_roots)}")
    if not summary.roots:
        print("\nNothing to scan.", file=sys.stderr)
        return 2

    print(
        f"\n{len(summary.ingested)} file(s) already in the database, holding "
        f"{human_bytes(summary.reclaimable_bytes)}."
    )
    print(
        f"{len(summary.not_ingested)} file(s) not loaded, holding "
        f"{human_bytes(summary.kept_bytes)} — these are still work to do."
    )

    folders = sorted(
        summary.by_directory.items(), key=lambda kv: -kv[1][1]
    )
    if folders:
        print(f"\nWhere the reclaimable space is (top {min(args.limit, len(folders))}):")
        for directory, (count, size) in folders[: args.limit]:
            print(f"  {human_bytes(size):>12}  {count:>5} file(s)  {directory}")
        if len(folders) > args.limit:
            print(f"  ... and {len(folders) - args.limit} more folder(s)")

    dupes = summary.duplicate_names
    if dupes:
        print(
            f"\n{len(dupes)} file name(s) appear in more than one folder. The "
            "database stores each once, so every copy reads as ingested — "
            "deleting all of them leaves none:"
        )
        for key, paths in list(dupes.items())[:10]:
            print(f"  {key}")
            for path in paths:
                print(f"      {path}")
        if len(dupes) > 10:
            print(f"  ... and {len(dupes) - 10} more")

    if args.output:
        written = write_listing(summary, args.output)
        print(f"\nWrote {written} path(s) to {args.output} — review before deleting.")

    print(
        "\nNothing was moved or deleted. To act on one folder:\n"
        "  hospitals archive-ingested <folder> --to <somewhere> --recursive --apply"
    )
    return 0


def _cmd_gap_report(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    require_schema(engine, args.database_url)
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
    if report.probable:
        pairs = len({(m.ccn, m.source_file) for m in report.probable})
        print(
            f"\n{pairs} of those file(s) resemble a hospital on the worklist "
            f"({report.recoverable_rows:,} charge rows) — see 'Probably Already "
            "Held'.\nThat is coverage already on disk. Try this before "
            "downloading anything:\n"
            "  hospitals fetch-crosswalk --database-url <url>\n"
            "  hospitals backfill-npis --apply --database-url <url>\n"
            "  hospitals link-charges --database-url <url>"
        )
    never = report.uncrawled_states
    if never:
        print(f"States with nothing ingested yet ({len(never)}): {', '.join(never)}")
    print(f"\nWrote {args.output}")
    return 0


def _cmd_discover_mrf(args: argparse.Namespace) -> int:
    """Ask each hospital's own website where its file is, and write it down.

    Deliberately downloads nothing. A manifest can be read, sorted, corrected
    and re-run; a crawl that discovers and downloads in one pass can only be
    repeated in full.
    """

    engine = make_engine(args.database_url)
    require_schema(engine, args.database_url)

    try:
        websites = load_websites(args.websites)
    except FileNotFoundError:
        print(
            f"\nNo website file at {args.websites}\n"
            "That file is the scorecard app's hospital profile — pass its path\n"
            "with --websites, or there is nothing to crawl from.",
            file=sys.stderr,
        )
        return 2

    summary = choose_targets(
        engine,
        websites,
        states=args.state,
        include_covered=args.include_covered,
        limit=args.limit,
    )

    print(
        f"\n{summary.in_scope} hospital(s) in scope: "
        f"{summary.already_covered} already have a file, "
        f"{summary.no_website} have no website on record, "
        f"{len(summary.targets)} to ask."
    )
    if not summary.targets:
        print("Nothing to do.")
        return 0

    fetch = _text_fetcher(args.timeout)
    cache: dict = {}
    rows: list[dict] = []
    counts: dict[str, int] = {}

    for n, target in enumerate(summary.targets, 1):
        for discovery in discover_one(target.as_dict(), fetch, cache=cache):
            rows.append(to_row(discovery))
            counts[discovery.status] = counts.get(discovery.status, 0) + 1
        if n % 25 == 0:
            print(f"  {n}/{len(summary.targets)}...", flush=True)

    with open(args.output, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MANIFEST_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    found = counts.get("found", 0)
    ambiguous = counts.get("ambiguous", 0)
    print()
    for status in sorted(counts):
        print(f"  {status:<12} {counts[status]}")
    print(f"\nWrote {args.output} ({len(rows)} row(s), {found} ready to download).")
    if ambiguous:
        print(
            f"\n{ambiguous} row(s) are ambiguous: a health system publishes one\n"
            "cms-hpt.txt for every facility it owns, and no name matched clearly.\n"
            "Picking wrong there is invisible afterwards — the file is valid, just\n"
            "somebody else's — so open the manifest, set the right row's status to\n"
            "'found', and delete the rest for that CCN."
        )
    if found:
        print(f"\nThen:  hospitals fetch-mrf {args.output} --dest <folder>")
    return 0


def _cmd_fetch_mrf(args: argparse.Namespace) -> int:
    with open(args.manifest, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    wanted = {"found"} | ({"ambiguous"} if args.include_ambiguous else set())
    todo = [r for r in rows if (r.get("status") or "").strip() in wanted and r.get("mrf_url")]
    skipped = len(rows) - len(todo)
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"\n{len(todo)} file(s) to fetch ({skipped} row(s) not ready).")
    if not todo:
        return 0

    opener = requests_opener(timeout=args.timeout)
    counts: dict[str, int] = {}
    total_bytes = 0

    for n, row in enumerate(todo, 1):
        # fetch_one handles the failures it knows about. This catches the ones
        # it does not, because a run that stops at file 40 of 123 has spent the
        # bandwidth and kept none of the record of what happened.
        try:
            result = fetch_one(
                row,
                args.dest,
                opener=opener,
                max_bytes=args.max_bytes,
                overwrite=args.overwrite,
            )
        except KeyboardInterrupt:
            print(f"\nStopped at {n}/{len(todo)}. Re-run to resume; "
                  "files already downloaded are skipped.")
            break
        except Exception as exc:  # noqa: BLE001
            result = Fetched(
                ccn=str(row.get("ccn") or ""),
                url=str(row.get("mrf_url") or ""),
                status="error",
                note=f"{type(exc).__name__}: {exc}",
            )
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "ok":
            total_bytes += result.bytes_written
        print(
            f"  [{n}/{len(todo)}] {row.get('ccn','')} {result.status}"
            + (f" — {result.note}" if result.note and result.status != "skipped" else "")
        )

    print()
    for status in sorted(counts):
        print(f"  {status:<10} {counts[status]}")
    print(f"\nDownloaded {total_bytes / 1e9:.2f} GB into {args.dest}")
    # The path is positional. Printing a flag that does not exist wastes the
    # first attempt of whoever copies this line, which is everyone.
    print(
        f"\nThen:  hospitals ingest-charges {args.dest} --skip-existing --continue-on-error\n"
        "       hospitals link-charges\n"
        "Every file here carries its CCN in the name, so linking is exact."
    )
    return 0


def _text_fetcher(timeout: int):
    """Fetch a small text file, returning None for anything that is not one."""

    import requests

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "MinervaAI-MRF-Discovery/1.0 (+price transparency research)",
            "Accept": "text/plain,*/*",
        }
    )

    def fetch(url: str):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
        except Exception:  # noqa: BLE001 - one unreachable host is a row, not a crash
            return None
        if response.status_code != 200:
            return None
        # cms-hpt.txt is a few hundred bytes. Anything large is a web page.
        return response.text[:200_000]

    return fetch


def _cmd_fetch_crosswalk(args: argparse.Namespace) -> int:
    from .crosswalk import HOSPITAL_ENROLLMENT_TITLE

    engine = make_engine(args.database_url)
    summary = fetch_crosswalk(
        engine, dataset_title=args.dataset_title or HOSPITAL_ENROLLMENT_TITLE
    )

    print(
        f"\nLoaded {summary.loaded:,} NPI->CCN pairs from "
        f"{summary.source_title} ({summary.source_modified}), "
        f"{summary.rows_read:,} rows read."
    )
    if summary.conflict_count:
        print(
            f"{summary.conflict_count} NPI(s) appear with more than one CCN; "
            "the first was kept."
        )
    print("\nNow re-link:  hospitals link-charges --database-url <url>")
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
    except EmptyDatabase as exc:
        # A wrong --database-url is the commonest way to reach this, and it
        # used to arrive as sixty lines of SQLAlchemy traceback ending in
        # "no such table". The path is the answer; print the path.
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
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
    if args.command == "backfill-npis":
        return _cmd_backfill_npis(args)
    if args.command == "archive-ingested":
        return _cmd_archive_ingested(args)
    if args.command == "price":
        return _cmd_price(args)
    if args.command == "coverage":
        return _cmd_coverage(args)
    if args.command == "scan-ingested":
        return _cmd_scan_ingested(args)
    if args.command == "discover-mrf":
        return _cmd_discover_mrf(args)
    if args.command == "fetch-mrf":
        return _cmd_fetch_mrf(args)
    if args.command == "gap-report":
        return _cmd_gap_report(args)
    if args.command == "fetch-crosswalk":
        return _cmd_fetch_crosswalk(args)
    if args.command == "load-crosswalk":
        return _cmd_load_crosswalk(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
