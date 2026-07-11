#!/usr/bin/env python3
"""
merge_website_captures.py

Fold any number of website-capture CSVs into the master hospital list,
producing one coverage table that shows every hospital's best-known website
and what is still missing.

Capture files are anything with `ccn` and `website_uri` columns: the AHD
extractor output, the free/paid resolver outputs, the verifier output, or a
hand-maintained manual-overrides CSV. Pass them in PRIORITY ORDER — the
first file containing a website for a CCN wins (put manual overrides first,
curated sources like AHD next, automated resolvers last).

USAGE
-----
    python scripts/merge_website_captures.py \
        --master hospital_input.csv \
        --captures websites_manual.csv hospital_websites_ahd.csv \
                   hospital_websites_verified.csv \
        --output hospital_website_master.csv

Prints per-state coverage so the remaining work is always visible. Rerun
whenever a new capture lands — the master file is regenerated from scratch,
so it is always consistent with its inputs.
"""

import argparse
import csv
import os
import sys

EXTRA_FIELDS = ["website_uri", "system_website", "operating_status", "website_source"]


def load_captures(paths: list[str]) -> dict[str, dict]:
    """Return {ccn: capture} honoring priority order (first hit wins)."""
    best: dict[str, dict] = {}
    for path in paths:
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            if "ccn" not in fields or "website_uri" not in fields:
                sys.exit(f"ERROR: {path} needs 'ccn' and 'website_uri' columns.")
            label = os.path.splitext(os.path.basename(path))[0]
            for row in reader:
                ccn = (row.get("ccn") or "").strip()
                url = (row.get("website_uri") or "").strip()
                if not ccn or not url or ccn in best:
                    continue
                best[ccn] = {
                    "website_uri": url,
                    "system_website": (row.get("system_website") or "").strip(),
                    "operating_status": (row.get("operating_status") or "").strip(),
                    "website_source": (row.get("source") or label).strip(),
                }
    return best


def main():
    parser = argparse.ArgumentParser(description="Merge website captures into the master hospital list.")
    parser.add_argument("--master", required=True, help="Master hospital CSV (converter output)")
    parser.add_argument("--captures", required=True, nargs="+", help="Capture CSVs in priority order")
    parser.add_argument("--output", default="hospital_website_master.csv")
    args = parser.parse_args()

    captures = load_captures(args.captures)

    with open(args.master, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        master_fields = reader.fieldnames or []
        hospitals = list(reader)

    unmatched = set(captures) - {h["ccn"] for h in hospitals}
    total_by_state: dict[str, int] = {}
    captured_by_state: dict[str, int] = {}

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=master_fields + EXTRA_FIELDS + ["capture_status"])
        writer.writeheader()
        for h in hospitals:
            state = h.get("state", "")
            total_by_state[state] = total_by_state.get(state, 0) + 1
            cap = captures.get(h["ccn"])
            row = dict(h, **(cap or {k: "" for k in EXTRA_FIELDS}))
            row["capture_status"] = "CAPTURED" if cap else "PENDING"
            if cap:
                captured_by_state[state] = captured_by_state.get(state, 0) + 1
            writer.writerow(row)

    captured = sum(captured_by_state.values())
    print(f"{captured}/{len(hospitals)} hospitals have a website ({len(hospitals) - captured} pending)")
    for state in sorted(k for k in captured_by_state):
        print(f"  {state}: {captured_by_state[state]}/{total_by_state[state]}")
    if unmatched:
        print(f"WARNING: {len(unmatched)} captured CCNs not in master list: {sorted(unmatched)[:10]}{'...' if len(unmatched) > 10 else ''}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
