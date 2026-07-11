#!/usr/bin/env python3
"""
resolve_hospital_websites_free.py

Zero-cost bulk website resolution for US hospitals from open data:

  1. Wikidata  — items that are hospitals in the US with an official
                 website (P856), fetched in one SPARQL query.
  2. OpenStreetMap — amenity=hospital objects with a website tag, fetched
                 per state through the Overpass API.

Both sources are free and licensed for reuse (CC0 / ODbL). Coverage is
PARTIAL — expect websites for roughly a third to half of hospitals, skewed
toward larger, active facilities. The point is sequencing: run this first,
then send only the *remaining* hospitals through the paid Google Places
resolver (scripts/resolve_hospital_websites.py), which then costs a
fraction of a full pass — or nothing, if you spread it across the Places
API's monthly free-call allowance using --limit.

USAGE
-----
    pip install requests
    python scripts/resolve_hospital_websites_free.py \
        --input hospital_input.csv \
        --output hospital_websites_free.csv \
        --remaining-output hospital_input_remaining.csv

    # then, for the gap only:
    python scripts/resolve_hospital_websites.py --input hospital_input_remaining.csv ...

INPUT / OUTPUT
--------------
Input: same CSV the paid resolver takes (ccn, hospital_name, address,
city, state, zip). Output: one row per input hospital that matched a
candidate, with the matched name, website, source, and a confidence flag.
The remaining-output file contains the input rows NOT resolved at HIGH
confidence (pass --accept-medium to also treat MEDIUM as resolved), in the
exact input schema, ready to feed to the paid resolver.

MATCHING
--------
There is no CCN in either source, so matching is name similarity within
the same state (plus city agreement when the source provides one), using
the same normalization as the paid resolver. Thresholds are conservative:
a wrong website is worse than a missing one. Candidates without a
resolvable state are only accepted on near-exact national name matches
and never marked HIGH.
"""

import argparse
import csv
import re
import sys
import time
from difflib import SequenceMatcher

import requests

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "hospitals-kb/0.1 (+https://github.com/KingdomandCrown/MedicaidData)"
REQUEST_TIMEOUT = 180
MAX_RETRIES = 4
BASE_BACKOFF = 3.0
OVERPASS_SLEEP = 2.0  # politeness delay between per-state Overpass queries

# Hospitals in the US with an official website; the state is resolved
# through the located-in chain (P131*) to a U.S. state item (Q35657).
WIKIDATA_QUERY = """
SELECT DISTINCT ?hospitalLabel ?website ?stateLabel WHERE {
  ?hospital wdt:P31/wdt:P279* wd:Q16917 ;
            wdt:P17 wd:Q30 ;
            wdt:P856 ?website .
  OPTIONAL { ?hospital wdt:P131* ?state . ?state wdt:P31 wd:Q35657 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

STATE_NAME_TO_USPS = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN",
    "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "AMERICAN SAMOA": "AS", "GUAM": "GU", "NORTHERN MARIANA ISLANDS": "MP",
    "PUERTO RICO": "PR", "U.S. VIRGIN ISLANDS": "VI", "VIRGIN ISLANDS": "VI",
}


def normalize(s: str) -> str:
    """Same normalization as the paid resolver, for consistent scoring."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_website(url: str) -> str:
    """OSM website tags sometimes lack a scheme; Wikidata's never do."""
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


class Candidate:
    __slots__ = ("name", "norm_name", "website", "state", "city", "source")

    def __init__(self, name: str, website: str, state: str | None, city: str | None, source: str):
        self.name = name
        self.norm_name = normalize(name)
        self.website = normalize_website(website)
        self.state = state
        self.city = city
        self.source = source


def _request_with_retries(method: str, url: str, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(BASE_BACKOFF * (2 ** (attempt - 1)))
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(BASE_BACKOFF * (2 ** (attempt - 1)))
    raise RuntimeError(f"exhausted retries for {url}: {last_exc}")


def parse_wikidata_bindings(bindings: list[dict]) -> list[Candidate]:
    """Turn SPARQL JSON bindings into candidates. A hospital can appear once
    per state binding; state-resolved rows win over stateless duplicates."""
    by_key: dict[tuple[str, str], Candidate] = {}
    for b in bindings:
        name = b.get("hospitalLabel", {}).get("value", "")
        website = b.get("website", {}).get("value", "")
        if not name or not website or name.startswith("Q"):  # unlabeled items
            continue
        state_label = b.get("stateLabel", {}).get("value", "")
        state = STATE_NAME_TO_USPS.get(state_label.strip().upper())
        cand = Candidate(name, website, state, None, "wikidata")
        key = (cand.norm_name, cand.website)
        existing = by_key.get(key)
        if existing is None or (existing.state is None and state is not None):
            by_key[key] = cand
    return list(by_key.values())


def fetch_wikidata() -> list[Candidate]:
    print("Fetching US hospital websites from Wikidata (one SPARQL query)...")
    resp = _request_with_retries(
        "GET",
        WIKIDATA_SPARQL_URL,
        params={"query": WIKIDATA_QUERY, "format": "json"},
        headers={"User-Agent": USER_AGENT},
    )
    bindings = resp.json()["results"]["bindings"]
    candidates = parse_wikidata_bindings(bindings)
    print(f"  {len(candidates)} Wikidata candidates")
    return candidates


def parse_overpass_elements(elements: list[dict], state_usps: str) -> list[Candidate]:
    out = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "")
        website = tags.get("website") or tags.get("contact:website") or ""
        if not name or not website:
            continue
        out.append(Candidate(name, website, state_usps, tags.get("addr:city"), "osm"))
    return out


def fetch_osm(states: list[str]) -> list[Candidate]:
    print(f"Fetching hospital website tags from OpenStreetMap ({len(states)} states)...")
    candidates: list[Candidate] = []
    for i, usps in enumerate(sorted(states)):
        query = (
            f'[out:json][timeout:120];'
            f'area["ISO3166-2"="US-{usps}"][admin_level=4]->.a;'
            f'nwr["amenity"="hospital"]["website"](area.a);'
            f'out tags;'
        )
        got = None
        for url in OVERPASS_URLS:
            try:
                resp = _request_with_retries(
                    "POST", url, data={"data": query}, headers={"User-Agent": USER_AGENT}
                )
                got = resp.json().get("elements", [])
                break
            except RuntimeError:
                continue
        if got is None:
            print(f"  WARNING: Overpass failed for {usps}; skipping that state")
            continue
        state_cands = parse_overpass_elements(got, usps)
        candidates.extend(state_cands)
        print(f"  [{i + 1}/{len(states)}] {usps}: {len(state_cands)} tagged hospitals")
        time.sleep(OVERPASS_SLEEP)
    return candidates


def score(input_name: str, input_city: str, cand: Candidate) -> tuple[float, str]:
    """Return (name_similarity, confidence). Mirrors the paid resolver's
    posture but is stricter, because there is no returned address to
    corroborate: name-only matches need to be near-exact for HIGH."""
    sim = SequenceMatcher(None, normalize(input_name), cand.norm_name).ratio()
    city_hit = bool(input_city and cand.city) and normalize(input_city) == normalize(cand.city)

    if cand.state is None:
        # National (stateless) candidates: near-exact names only, never HIGH.
        return sim, "MEDIUM" if sim >= 0.93 else "NONE"
    if sim >= 0.90 or (sim >= 0.72 and city_hit):
        return sim, "HIGH"
    if sim >= 0.72:
        return sim, "MEDIUM"
    if sim >= 0.55:
        return sim, "LOW"
    return sim, "NONE"


def best_match(row: dict, by_state: dict, stateless: list) -> tuple[Candidate, float, str] | None:
    pool = by_state.get(row["state"].upper(), []) + stateless
    best = None
    for cand in pool:
        sim, conf = score(row["hospital_name"], row.get("city", ""), cand)
        if conf == "NONE":
            continue
        rank = ({"HIGH": 2, "MEDIUM": 1, "LOW": 0}[conf], sim)
        if best is None or rank > best[0]:
            best = (rank, cand, sim, conf)
    if best is None:
        return None
    _, cand, sim, conf = best
    return cand, sim, conf


def main():
    parser = argparse.ArgumentParser(
        description="Resolve hospital websites from free sources (Wikidata + OSM)."
    )
    parser.add_argument("--input", required=True, help="Resolver input CSV (ccn, hospital_name, ...)")
    parser.add_argument("--output", default="hospital_websites_free.csv")
    parser.add_argument(
        "--remaining-output",
        default="hospital_input_remaining.csv",
        help="Input rows not resolved at HIGH confidence, for the paid resolver",
    )
    parser.add_argument("--skip-wikidata", action="store_true")
    parser.add_argument("--skip-osm", action="store_true")
    parser.add_argument(
        "--accept-medium",
        action="store_true",
        help="Treat MEDIUM matches as resolved (excluded from remaining-output)",
    )
    args = parser.parse_args()

    with open(args.input, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        # Preserve the input schema verbatim (incl. extra columns like phone)
        # so remaining-output feeds straight back into any downstream step.
        input_fields = reader.fieldnames or [
            "ccn", "hospital_name", "address", "city", "state", "zip",
        ]
    states = sorted({r["state"].upper() for r in rows if r.get("state")})
    print(f"{len(rows)} hospitals across {len(states)} states in {args.input}")

    candidates: list[Candidate] = []
    if not args.skip_wikidata:
        candidates += fetch_wikidata()
    if not args.skip_osm:
        candidates += fetch_osm(states)
    if not candidates:
        sys.exit("No candidates fetched — check network access to Wikidata/Overpass.")

    by_state: dict[str, list[Candidate]] = {}
    stateless: list[Candidate] = []
    for cand in candidates:
        if cand.state:
            by_state.setdefault(cand.state, []).append(cand)
        else:
            stateless.append(cand)

    resolved_levels = {"HIGH", "MEDIUM"} if args.accept_medium else {"HIGH"}
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    remaining: list[dict] = []

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "ccn", "hospital_name", "city", "state", "matched_name",
                "matched_city", "website_uri", "source", "name_similarity",
                "match_confidence",
            ],
        )
        writer.writeheader()
        for row in rows:
            match = best_match(row, by_state, stateless)
            if match is None:
                remaining.append(row)
                continue
            cand, sim, conf = match
            counts[conf] += 1
            writer.writerow({
                "ccn": row["ccn"],
                "hospital_name": row["hospital_name"],
                "city": row.get("city", ""),
                "state": row.get("state", ""),
                "matched_name": cand.name,
                "matched_city": cand.city or "",
                "website_uri": cand.website,
                "source": cand.source,
                "name_similarity": f"{sim:.3f}",
                "match_confidence": conf,
            })
            if conf not in resolved_levels:
                remaining.append(row)

    with open(args.remaining_output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=input_fields)
        writer.writeheader()
        for row in remaining:
            writer.writerow({k: row.get(k, "") for k in input_fields})

    print(f"\nMatches: {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW")
    print(f"Resolved (excluded from remaining): {len(rows) - len(remaining)}")
    print(f"Remaining for paid/manual resolution: {len(remaining)} -> {args.remaining_output}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
