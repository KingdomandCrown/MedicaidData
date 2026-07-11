#!/usr/bin/env python3
"""
verify_hospital_websites.py

Verify resolved hospital websites by fetching each candidate page and
checking it against facts we already know about the hospital.

WHY
---
No resolution source (Google Places, Wikidata, OSM) is 100% correct, and
name-similarity confidence flags only say the *listing* matched — not that
the URL is right, alive, or actually the hospital's own site. This script
turns "probably right" into evidence:

  1. Cross-source agreement — the same registrable domain returned by two
     independent sources is strong evidence by itself.
  2. Content verification — fetch the page and look for the hospital's
     name tokens, city, ZIP, and phone number in the page text. A phone
     match is near-conclusive.
  3. Sanity checks — flag aggregator/directory domains (Facebook, Yelp,
     Healthgrades, Wikipedia, ...) that resolvers sometimes return, and
     record dead/parked/unreachable URLs.

VERDICTS (one row per hospital in --hospitals)
----------------------------------------------
  VERIFIED      strong content match, or >= 2 sources agree on a live,
                non-aggregator domain
  REVIEW        reachable but evidence is weak, conflicting, or the domain
                is an aggregator — route to manual review
  DEAD          every candidate URL was unreachable
  NO_CANDIDATE  no source produced a website for this hospital — decide
                manually whether one exists (many closed hospitals have none)

USAGE
-----
    python scripts/verify_hospital_websites.py \
        --hospitals hospital_input.csv \
        --results hospital_websites_free.csv hospital_websites_resolved.csv \
        --output hospital_websites_verified.csv

Any results CSV with `ccn` and `website_uri` columns works; rows with an
empty website are ignored. LOW-confidence resolver matches are worth
including — verification is exactly the tool that can redeem them. The
output file doubles as a checkpoint: rerunning skips already-verified CCNs,
so a multi-hour run over ~10k hospitals can be stopped and resumed.
Fetching is polite but parallel (default 8 workers); expect roughly
30-60 minutes for 10k hospitals.
"""

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

USER_AGENT = "hospitals-kb/0.1 (+https://github.com/KingdomandCrown/MedicaidData)"
REQUEST_TIMEOUT = 20
MAX_PAGE_BYTES = 600_000

# Directory/social/reference domains that are never a hospital's own site.
AGGREGATOR_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "wikipedia.org", "wikidata.org", "yelp.com",
    "healthgrades.com", "vitals.com", "webmd.com", "zocdoc.com",
    "medicare.gov", "cms.gov", "google.com", "goo.gl", "yellowpages.com",
    "ahd.com", "npidb.org", "npino.com", "hospital-data.com", "doctor.com",
    "caredash.com", "wellness.com", "findatopdoc.com", "mapquest.com",
}

# Generic tokens that carry no identity signal in a hospital name.
NAME_STOPWORDS = {
    "hospital", "medical", "center", "centre", "health", "healthcare",
    "regional", "community", "county", "memorial", "system", "clinic",
    "care", "the", "of", "and", "at", "for", "inc", "llc",
}

SOURCE_PRIORITY = {"places": 0, "wikidata": 1, "osm": 2}


def normalize(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def registrable_domain(url: str) -> str:
    """Naive eTLD+1: last two labels of the host, www stripped. Good enough
    for US hospital domains (.com/.org/.net/.gov/.edu/.health)."""
    try:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except ValueError:
        return ""
    host = host[4:] if host.startswith("www.") else host
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_aggregator(url: str) -> bool:
    return registrable_domain(url) in AGGREGATOR_DOMAINS


def infer_source(fieldnames: list[str], path: str) -> str:
    if "source" in fieldnames:
        return ""  # per-row source column present
    if "google_place_id" in fieldnames:
        return "places"
    return os.path.splitext(os.path.basename(path))[0]


def load_candidates(result_paths: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Return {ccn: [(url, source), ...]} across all result files."""
    candidates: dict[str, list[tuple[str, str]]] = {}
    for path in result_paths:
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            if "ccn" not in fields or "website_uri" not in fields:
                sys.exit(f"ERROR: {path} needs 'ccn' and 'website_uri' columns.")
            default_source = infer_source(fields, path)
            for row in reader:
                url = (row.get("website_uri") or "").strip()
                ccn = (row.get("ccn") or "").strip()
                if not url or not ccn:
                    continue
                source = (row.get("source") or default_source or "unknown").strip()
                candidates.setdefault(ccn, []).append((url, source))
    return candidates


def rank_candidates(cands: list[tuple[str, str]]) -> list[dict]:
    """Group candidate URLs by registrable domain; order by cross-source
    agreement (more independent sources first), then source priority."""
    by_domain: dict[str, dict] = {}
    for url, source in cands:
        dom = registrable_domain(url)
        if not dom:
            continue
        entry = by_domain.setdefault(dom, {"domain": dom, "urls": [], "sources": set()})
        if url not in entry["urls"]:
            entry["urls"].append(url)
        entry["sources"].add(source)
    ranked = sorted(
        by_domain.values(),
        key=lambda e: (
            -len(e["sources"]),
            min(SOURCE_PRIORITY.get(s, 9) for s in e["sources"]),
        ),
    )
    return ranked


def page_text(html: str) -> str:
    """Crude but dependency-free text extraction."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return normalize(html)


def evaluate_page(html: str, hospital: dict) -> dict:
    """Check the hospital's known facts against the page text."""
    text = page_text(html[:MAX_PAGE_BYTES])
    digits = re.sub(r"\D", "", html[:MAX_PAGE_BYTES])

    tokens = [t for t in normalize(hospital["hospital_name"]).split() if t not in NAME_STOPWORDS]
    if not tokens:
        tokens = normalize(hospital["hospital_name"]).split()
    hits = sum(1 for t in tokens if t in text)
    name_match = bool(tokens) and hits / len(tokens) >= 0.6

    city = normalize(hospital.get("city", ""))
    zip5 = (hospital.get("zip") or "").strip()
    phone = re.sub(r"\D", "", hospital.get("phone", "") or "")

    return {
        "name_match": name_match,
        "city_match": bool(city) and city in text,
        "zip_match": bool(zip5) and zip5 in digits,
        "phone_match": len(phone) == 10 and phone in digits,
    }


def decide(reachable: bool, aggregator: bool, agreement: int, checks: dict) -> str:
    if not reachable:
        return "DEAD"
    if aggregator:
        return "REVIEW"
    strong = checks["name_match"] and (
        checks["city_match"] or checks["zip_match"] or checks["phone_match"]
    )
    # Phone is near-conclusive even when a JS-heavy page hides the name text.
    if strong or checks["phone_match"] or agreement >= 2:
        return "VERIFIED"
    return "REVIEW"


def fetch(url: str, session: requests.Session) -> tuple[int, str, str, str]:
    """Return (status, final_url, html, error)."""
    try:
        resp = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
        content = resp.raw.read(MAX_PAGE_BYTES, decode_content=True) or b""
        html = content.decode(resp.encoding or "utf-8", errors="replace")
        return resp.status_code, resp.url, html, ""
    except requests.RequestException as exc:
        return 0, url, "", str(exc)[:200]


def verify_one(hospital: dict, cands: list[tuple[str, str]], session: requests.Session) -> dict:
    base = {
        "ccn": hospital["ccn"],
        "hospital_name": hospital["hospital_name"],
        "city": hospital.get("city", ""),
        "state": hospital.get("state", ""),
        "website_uri": "", "final_url": "", "domain": "", "sources": "",
        "source_agreement": 0, "http_status": "", "name_match": "",
        "city_match": "", "zip_match": "", "phone_match": "",
        "aggregator": "", "verdict": "NO_CANDIDATE", "error": "",
    }
    ranked = rank_candidates(cands)
    if not ranked:
        return base

    best_row = None
    for entry in ranked:
        url = sorted(entry["urls"], key=lambda u: not u.lower().startswith("https"))[0]
        status, final_url, html, error = fetch(url, session)
        reachable = 200 <= status < 400 and bool(html)
        aggregator = is_aggregator(final_url or url)
        checks = (
            evaluate_page(html, hospital)
            if reachable
            else {"name_match": False, "city_match": False, "zip_match": False, "phone_match": False}
        )
        verdict = decide(reachable, aggregator, len(entry["sources"]), checks)
        row = dict(
            base,
            website_uri=url,
            final_url=final_url,
            domain=entry["domain"],
            sources="|".join(sorted(entry["sources"])),
            source_agreement=len(entry["sources"]),
            http_status=status,
            name_match=checks["name_match"],
            city_match=checks["city_match"],
            zip_match=checks["zip_match"],
            phone_match=checks["phone_match"],
            aggregator=aggregator,
            verdict=verdict,
            error=error,
        )
        if verdict == "VERIFIED":
            return row
        # Keep the most useful non-verified row: REVIEW beats DEAD.
        if best_row is None or (verdict == "REVIEW" and best_row["verdict"] == "DEAD"):
            best_row = row
    return best_row or base


OUTPUT_FIELDS = [
    "ccn", "hospital_name", "city", "state", "website_uri", "final_url",
    "domain", "sources", "source_agreement", "http_status", "name_match",
    "city_match", "zip_match", "phone_match", "aggregator", "verdict", "error",
]


def main():
    parser = argparse.ArgumentParser(description="Verify resolved hospital websites.")
    parser.add_argument("--hospitals", required=True, help="Hospital facts CSV (converter output)")
    parser.add_argument("--results", required=True, nargs="+", help="Resolver output CSV(s)")
    parser.add_argument("--output", default="hospital_websites_verified.csv")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="Cap hospitals verified this run")
    args = parser.parse_args()

    with open(args.hospitals, "r", newline="", encoding="utf-8") as f:
        hospitals = [r for r in csv.DictReader(f) if r.get("ccn")]
    candidates = load_candidates(args.results)

    done = set()
    if os.path.exists(args.output):
        with open(args.output, "r", newline="", encoding="utf-8") as f:
            done = {r["ccn"] for r in csv.DictReader(f)}
    todo = [h for h in hospitals if h["ccn"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(hospitals)} hospitals; {len(done)} already verified; {len(todo)} this run")

    out_exists = os.path.exists(args.output)
    counts: dict[str, int] = {}
    started = time.time()
    with open(args.output, "a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=OUTPUT_FIELDS)
        if not out_exists:
            writer.writeheader()
        session = requests.Session()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(verify_one, h, candidates.get(h["ccn"], []), session): h["ccn"]
                for h in todo
            }
            for i, future in enumerate(as_completed(futures), 1):
                row = future.result()
                counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
                writer.writerow(row)
                out.flush()
                if i % 100 == 0:
                    rate = i / (time.time() - started)
                    print(f"  {i}/{len(todo)} ({rate:.1f}/s) | " + ", ".join(
                        f"{k}={v}" for k, v in sorted(counts.items())))

    print("\nDone. " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Output: {args.output}")
    print("Next: manually review verdict=REVIEW rows; decide NO_CANDIDATE/DEAD rows;")
    print("record decisions in a manual-overrides CSV that wins over automated data.")


if __name__ == "__main__":
    main()
