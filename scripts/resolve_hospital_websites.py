#!/usr/bin/env python3
"""
resolve_hospital_websites.py

Bulk-resolve official websites for US hospitals using the Google Places API (New).

WHY THIS APPROACH
------------------
No free federal dataset (NPPES, POS, Care Compare) carries a hospital website
field at CCN grain. Google Places is the most scalable path to close that gap:
one Text Search call per hospital, using name + city + state as the query,
requesting the `websiteUri` field directly in the response (no separate
Place Details call needed).

WHAT THIS SCRIPT DOES
----------------------
1. Reads your hospital list (CCN, name, address, city, state[, zip]).
2. Calls Places API (New) Text Search for each hospital.
3. Captures the website, the matched place name/address, and a basic
   match-confidence flag so you can route low-confidence hits to manual
   review -- the same posture as your existing hospital_website_candidates
   table, but starting from a much higher hit rate.
4. Writes results incrementally to CSV and checkpoints progress, so a
   6,000+ row run can be safely stopped and resumed.
5. Rate-limits and retries with backoff on transient errors (429/5xx).

SETUP
-----
1. In Google Cloud Console: create/select a project, enable "Places API (New)",
   enable billing, create an API key, and (recommended) restrict the key to
   the Places API and to your server's IP.
2. Set a daily quota cap and a budget alert in Cloud Console before running
   at scale -- this is the single most important cost control.
3. pip install requests --break-system-packages
4. export GOOGLE_PLACES_API_KEY="your-key-here"

COST NOTE
---------
Requesting `websiteUri` puts each call on the Enterprise SKU tier (as of
mid-2026, roughly $35-40 per 1,000 calls depending on volume tier --
verify current pricing at https://mapsplatform.google.com/pricing/ before
a full run, since Google revises SKU pricing periodically). For ~6,175
hospitals, budget on the order of $200-250 for a single complete pass.
Re-running only fills gaps if you keep the checkpoint file, so the full
cost is a one-time cost, not a recurring one -- websites don't change
often enough to justify anything more than a periodic refresh (quarterly
or semi-annual) of the whole file.

INPUT CSV
---------
Expected columns (adapt HOSPITAL_NAME_COL etc. below if yours differ):
    ccn, hospital_name, address, city, state, zip

OUTPUT CSV
----------
    ccn, query_used, google_place_id, google_name, google_formatted_address,
    website_uri, business_status, match_confidence, http_status, error
"""

import csv
import os
import re
import sys
import time
import argparse
from difflib import SequenceMatcher

import requests

# ---- Column mapping: adjust to match your export from `hospitals` ----
CCN_COL = "ccn"
HOSPITAL_NAME_COL = "hospital_name"
ADDRESS_COL = "address"        # optional; leave blank string in CSV if unavailable
CITY_COL = "city"
STATE_COL = "state"
ZIP_COL = "zip"                # optional

PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Only request the fields you need -- this keeps you on a single, predictable
# SKU tier instead of accidentally pulling in reviews/ratings (Enterprise+Atmosphere).
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.businessStatus",
    "places.nationalPhoneNumber",
])

REQUEST_TIMEOUT = 15
MAX_RETRIES = 5
BASE_BACKOFF = 2.0          # seconds; doubles each retry
REQUESTS_PER_SECOND = 8     # stay comfortably under default QPS limits


def normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- for fuzzy match scoring."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_confidence(input_name: str, input_city: str, google_name: str, google_address: str) -> str:
    """
    Cheap, dependency-free confidence heuristic:
      - HIGH:   name similarity >= 0.72 AND city text appears in the returned address
      - MEDIUM: name similarity >= 0.55, or city match without strong name match
      - LOW:    everything else -- route to manual review, same as your
                existing website_candidates workflow
    This is intentionally conservative. Tune thresholds against a sample of
    known-good hospitals before a full production run.
    """
    name_sim = SequenceMatcher(None, normalize(input_name), normalize(google_name)).ratio()
    city_hit = normalize(input_city) in normalize(google_address)

    if name_sim >= 0.72 and city_hit:
        return "HIGH"
    if name_sim >= 0.55 or city_hit:
        return "MEDIUM"
    return "LOW"


def build_query(name: str, address: str, city: str, state: str, zip_code: str) -> str:
    parts = [name]
    if address:
        parts.append(address)
    parts += [p for p in (city, state, zip_code) if p]
    parts.append("hospital")  # nudges Places toward the medical facility over similarly-named businesses
    return " ".join(parts)


def call_places_text_search(api_key: str, query: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    payload = {"textQuery": query, "maxResultCount": 1}

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(PLACES_TEXT_SEARCH_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return {"status": 200, "body": resp.json(), "error": ""}
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = BASE_BACKOFF * (2 ** (attempt - 1))
                time.sleep(wait)
                continue
            # Non-retryable error (4xx other than 429)
            return {"status": resp.status_code, "body": {}, "error": resp.text[:300]}
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(BASE_BACKOFF * (2 ** (attempt - 1)))
    return {"status": 0, "body": {}, "error": f"exhausted retries: {last_exc}"}



def load_checkpoint(checkpoint_path: str) -> set:
    done = set()
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row["ccn"])
    return done


def main():
    parser = argparse.ArgumentParser(description="Resolve hospital websites via Google Places API (New).")
    parser.add_argument("--input", required=True, help="Path to input hospital CSV")
    parser.add_argument("--output", default="hospital_websites_resolved.csv", help="Path to output/checkpoint CSV")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap on number of hospitals to process (for a test run)")
    parser.add_argument("--sleep", type=float, default=1.0 / REQUESTS_PER_SECOND, help="Seconds to sleep between calls")
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        sys.exit("ERROR: set GOOGLE_PLACES_API_KEY in your environment before running.")

    already_done = load_checkpoint(args.output)
    print(f"Resuming: {len(already_done)} hospitals already resolved in {args.output}")

    out_exists = os.path.exists(args.output)
    out_file = open(args.output, "a", newline="", encoding="utf-8")
    fieldnames = [
        "ccn", "query_used", "google_place_id", "google_name",
        "google_formatted_address", "website_uri", "business_status",
        "match_confidence", "http_status", "error",
    ]
    writer = csv.DictWriter(out_file, fieldnames=fieldnames)
    if not out_exists:
        writer.writeheader()

    with open(args.input, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if args.limit:
        rows = rows[: args.limit]

    total = len(rows)
    processed = 0
    resolved = 0
    low_conf = 0

    for row in rows:
        ccn = row.get(CCN_COL, "").strip()
        if not ccn or ccn in already_done:
            continue

        name = row.get(HOSPITAL_NAME_COL, "").strip()
        address = row.get(ADDRESS_COL, "").strip()
        city = row.get(CITY_COL, "").strip()
        state = row.get(STATE_COL, "").strip()
        zip_code = row.get(ZIP_COL, "").strip()

        query = build_query(name, address, city, state, zip_code)
        result = call_places_text_search(api_key, query)

        place = {}
        places_list = result["body"].get("places", [])
        if places_list:
            place = places_list[0]

        google_name = place.get("displayName", {}).get("text", "")
        google_address = place.get("formattedAddress", "")
        website = place.get("websiteUri", "")
        status = place.get("businessStatus", "")
        place_id = place.get("id", "")

        conf = match_confidence(name, city, google_name, google_address) if place else "LOW"
        if conf == "LOW":
            low_conf += 1
        if website:
            resolved += 1

        writer.writerow({
            "ccn": ccn,
            "query_used": query,
            "google_place_id": place_id,
            "google_name": google_name,
            "google_formatted_address": google_address,
            "website_uri": website,
            "business_status": status,
            "match_confidence": conf,
            "http_status": result["status"],
            "error": result["error"],
        })
        out_file.flush()

        processed += 1
        if processed % 100 == 0:
            print(f"  {processed}/{total} processed | {resolved} websites found | {low_conf} low-confidence")

        time.sleep(args.sleep)

    out_file.close()
    print(f"\nDone. Processed {processed} hospitals this run.")
    print(f"Websites found: {resolved}")
    print(f"Low-confidence matches (route to manual review): {low_conf}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
