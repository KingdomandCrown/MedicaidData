# Hospitals — a public U.S. hospital knowledge base

A small, reproducible pipeline that builds a knowledge base of U.S. hospitals
from public federal data. The first data source is the **CMS Provider of
Services (POS) file**, which lists every Medicare/Medicaid-certified provider
in the country.

Coverage is state-parameterized: **Kansas** is the default, any other state
works by passing a different `--state`, and `--state ALL` ingests **every US
hospital** in a single national pass.

The pipeline:

1. **Downloads** the latest CMS Provider of Services file.
2. **Filters** it to active hospitals in the target state.
3. **Normalizes** identifiers (CCN, ZIP, phone) and decodes coded fields
   (provider subtype, ownership, active/terminated status).
4. **Loads** the result into SQLite (default) or a PostgreSQL-compatible
   database, recording a provenance row for every run.

---

## Quick start

```bash
# 1. Install (Python 3.10+)
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt

# 2. Ingest active Kansas hospitals from the latest CMS POS file into SQLite
hospitals ingest --state KS

# 3. Inspect what was loaded
hospitals stats --state KS
```

By default the data lands in `data/hospitals.sqlite`. Then do the same for
Maryland:

```bash
hospitals ingest --state MD
```

### Ingesting all US hospitals

Pass `--state ALL` (aliases: `US`, `USA`, `NATIONAL`) to skip the state filter
and load the whole country — roughly 6,000+ active hospitals — in one run:

```bash
hospitals ingest --state ALL
hospitals stats            # national count
hospitals stats --state TX # per-state count
```

The data-api still filters server-side to hospitals (provider category `01`),
so a national run downloads only hospital rows (a few thousand, paged 1,000 at
a time), not the full ~100 MB POS file. Because the loader upserts by CCN, a
national run can safely follow (or be followed by) per-state runs — rows are
updated in place, never duplicated. The same works offline against a
downloaded POS CSV: `hospitals ingest --state ALL --input-file /path/to/pos.csv`.

A national run records `NULL` in the `ingestion_runs.state` provenance column
(the column holds 2-char state codes).

> If you have not installed the package (`pip install -e .`), run the CLI as a
> module instead: `PYTHONPATH=src python -m hospitals.cli ingest --state KS`.

### Loading into PostgreSQL

The schema and the upsert logic are portable across SQLite and PostgreSQL.
Point `--database-url` at any SQLAlchemy URL:

```bash
pip install -e '.[postgres]'   # installs psycopg
hospitals ingest --state KS \
  --database-url "postgresql+psycopg://user:pass@localhost:5432/hospitals"
```

On both engines the loader performs a native `INSERT ... ON CONFLICT` upsert
keyed by CCN, so re-running an ingest updates existing rows instead of
duplicating them.

### Running offline / behind a restricted network

The default path discovers and streams the latest file directly from
`data.cms.gov`. If outbound access to CMS is blocked (corporate proxy,
sandboxed CI, egress policy), download the POS **Hospital & Non-Hospital
Facilities** CSV manually from
<https://data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file-hospital-non-hospital-facilities>
and point the pipeline at the local file:

```bash
hospitals ingest --state KS --input-file /path/to/pos.csv
```

The CSV and the API expose the same uppercase column names, so both paths
produce identical results.

---

## CLI reference

```
hospitals ingest [options]
  --state STATE            USPS code or name to ingest, or ALL for every
                           US hospital (default: KS)
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --input-file PATH        Read a local POS CSV instead of downloading from CMS
  --include-inactive       Keep providers whose termination code is not "active"
  --limit N                Cap on raw records read (smoke testing)
  --echo-sql               Echo SQL statements (debugging)

hospitals stats [--state STATE] [--database-url URL]
  Show hospital row counts in the database.

Global:
  --log-level LEVEL        DEBUG | INFO | WARNING | ...  (or $HOSPITALS_LOG_LEVEL)
  --version
```

Every run logs its progress (discovery, download, row counts, upsert) and
writes a row to the `ingestion_runs` table for provenance.

---

## Data model

### `hospitals`

One row per provider, keyed by the normalized 6-character CCN.

| column | notes |
|---|---|
| `ccn` | **PK.** CMS Certification Number, normalized to 6 chars |
| `name` | facility name |
| `provider_category_code` | `01` = Hospital |
| `provider_subtype_code` / `provider_subtype` | e.g. `01` → "Short-term (Acute Care)", `09` → "Critical Access Hospital" |
| `address`, `city`, `state`, `zip5`, `county` | ZIP normalized to 5 digits |
| `phone` | digits only, 10-digit where possible |
| `ownership_code` / `ownership_type` | decoded from `GNRL_CNTL_TYPE_CD` |
| `certified_bed_count` | integer |
| `certification_date` | parsed date |
| `termination_code` / `is_active` | `is_active` is true when the POS termination code is `00` |
| `ssa_state_code`, `fips_state_code` | SSA code is the CCN prefix (Kansas = `17`, Maryland = `21`) |
| `source`, `source_edition`, `ingested_at` | provenance |

### `ingestion_runs`

One row per ingest: source, state, edition, records read/loaded, timestamps,
and status.

---

## How identifiers are normalized

- **CCN** (`PRVDR_NUM`): trimmed, upper-cased, and left-padded to 6 digits when
  a numeric CCN lost a leading zero. The leading two digits are the SSA state
  code, which we use to cross-check / backfill the state.
- **State**: taken from `STATE_CD`; if missing, derived from the CCN's SSA
  prefix.
- **ZIP**: reduced to the 5-digit ZIP, dropping any ZIP+4 suffix.
- **Phone**: stripped to digits, dropping a leading US country code.
- **Active status**: `PGM_TRMNTN_CD == "00"`.

A hospital is any record with `PRVDR_CTGRY_CD == "01"`. Non-hospital providers
(e.g. skilled nursing facilities) are filtered out.

---

## Project layout

```
src/hospitals/
  cli.py            argparse CLI (ingest, stats)
  ingest.py         orchestration: fetch -> filter -> normalize -> load
  cms_pos.py        CMS discovery + data-api / CSV / local-file fetching
  normalize.py      POS column mapping, identifier normalization, code lookups
  db.py             SQLAlchemy schema + dialect-aware upsert (SQLite/Postgres)
  states.py         USPS / SSA / FIPS state reference data
  logging_config.py logging setup
scripts/
  resolve_hospital_websites.py  bulk website lookup via Google Places API (New)
tests/
  fixtures/pos_sample.csv   representative POS rows (synthetic)
  test_normalize.py         identifier + code normalization
  test_cms_pos.py           discovery, pagination, CSV reading
  test_ingest.py            end-to-end into SQLite
  test_resolve_websites.py  website-resolver helpers (offline)
```

---

## Resolving hospital websites (Google Places)

No free federal dataset (NPPES, POS, Care Compare) carries a hospital website
field at CCN grain. `scripts/resolve_hospital_websites.py` closes that gap
with one Google Places API (New) Text Search call per hospital, requesting
`websiteUri` directly. It writes results incrementally with a checkpoint, so
a large run can be stopped and resumed, and flags each match `HIGH` /
`MEDIUM` / `LOW` confidence so low-confidence hits can be routed to manual
review.

1. In Google Cloud Console: enable "Places API (New)" and billing, create an
   API key, and set a daily quota cap + budget alert. **Cost:** requesting
   `websiteUri` bills on the Enterprise SKU (~$35–40 per 1,000 calls as of
   mid-2026 — verify at <https://mapsplatform.google.com/pricing/>), so a
   full national pass (~6,175 hospitals) is roughly $200–250, one time.
2. Make sure the database has the hospitals you want websites for — for a
   national pass, run `hospitals ingest --state ALL` first. Then export the
   input CSV, aliasing columns to the headers the script expects
   (`ccn, hospital_name, address, city, state, zip`):

   ```bash
   sqlite3 -header -csv data/hospitals.sqlite \
     "SELECT ccn, name AS hospital_name, address, city, state, zip5 AS zip
      FROM hospitals WHERE is_active = 1;" > hospital_input.csv
   ```

3. Run (start with `--limit` to validate query quality before a full pass):

   ```bash
   export GOOGLE_PLACES_API_KEY="your-key-here"
   python scripts/resolve_hospital_websites.py \
     --input hospital_input.csv \
     --output hospital_websites_resolved.csv \
     --limit 25
   ```

The output CSV doubles as the checkpoint: re-running with the same `--output`
skips CCNs already resolved and only fills gaps.

### Zero-cost resolution first (Wikidata + OpenStreetMap)

Before paying for a Places pass, `scripts/resolve_hospital_websites_free.py`
resolves what it can from free open data: Wikidata items with an official
website (one SPARQL query) and OpenStreetMap `amenity=hospital` objects with
a `website` tag (one Overpass query per state). Coverage is partial —
roughly a third to half of hospitals, skewed toward larger active
facilities — but every hit is one you never pay Google for:

```bash
python scripts/resolve_hospital_websites_free.py \
  --input hospital_input.csv \
  --output hospital_websites_free.csv \
  --remaining-output hospital_input_remaining.csv
```

Matching is name-similarity within the same state (there is no CCN in
either source), with stricter thresholds than the paid resolver since
there's no returned address to corroborate. The `--remaining-output` file
contains the hospitals *not* resolved at HIGH confidence, in the input
schema — feed exactly that file to the paid resolver so you only pay for
the gap (add `--accept-medium` to also trust MEDIUM matches).

The remaining gap can also be closed for free, slowly: Places API (New)
Enterprise SKUs include a monthly free-call allowance (about 1,000 calls/mo
as of the March 2025 pricing model — verify in your Cloud Console). Run the
paid resolver with `--limit 1000` once a month against the same `--output`
checkpoint and the whole file resolves over a few months at $0.

### Verifying correctness

Resolution (from any source) only says a *listing* matched — not that the
URL is right, alive, or the hospital's own site. `scripts/verify_hospital_websites.py`
turns resolved candidates into evidence-backed verdicts by fetching every
candidate page and checking it against facts already known about the
hospital (name tokens, city, ZIP, phone — the input's `phone` column exists
for this; a phone match is near-conclusive). It also rewards cross-source
agreement (the same domain from two independent sources) and flags
aggregator domains (Facebook, Yelp, Healthgrades, ...) that resolvers
sometimes return:

```bash
python scripts/verify_hospital_websites.py \
  --hospitals hospital_input.csv \
  --results hospital_websites_free.csv hospital_websites_resolved.csv \
  --output hospital_websites_verified.csv
```

Every hospital gets a verdict: `VERIFIED` (strong page evidence or 2+
sources agree on a live non-aggregator domain), `REVIEW` (reachable but
weak/conflicting evidence), `DEAD` (nothing reachable), or `NO_CANDIDATE`
(no source found anything — common for closed hospitals, where "no website"
is itself the correct answer). Only REVIEW/DEAD/NO_CANDIDATE rows need a
human; record those decisions in a manual-overrides CSV that always wins
over automated data. The output doubles as a checkpoint, so the multi-hour
fetch over ~10k sites can be stopped and resumed. Feeding LOW-confidence
resolver matches in is encouraged — verification is exactly the tool that
can redeem them.

### Starting from a spreadsheet instead of the database

If the hospital list lives in a per-state Excel workbook (AHD-style "Table of
Search Results" export, one sheet per state) rather than in the database,
`scripts/xlsx_to_resolver_input.py` converts it into the resolver's input CSV
directly:

```bash
pip install openpyxl
python scripts/xlsx_to_resolver_input.py \
  --input CMS_Hospital_List_All_USA.xlsx \
  --output hospital_input.csv
```

It locates each sheet's header row (preamble length varies), normalizes CCNs
and ZIPs to knowledge-base rules (zero-padding, upper-cased federal CCNs),
dedupes by CCN, and writes rows lacking a CCN to a `*_no_ccn.csv` sidecar for
manual review rather than dropping them silently. The export carries no
street address, so `address` is blank — the resolver treats it as optional.

---

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests are hermetic: they run the full pipeline against
`tests/fixtures/pos_sample.csv` (synthetic, format-accurate POS rows) and never
touch the network. The CMS discovery and pagination logic is tested with
mocked HTTP responses.

---

## Roadmap

- [x] Kansas — CMS Provider of Services ingestion
- [x] Maryland — same pipeline, `--state MD`
- [x] Additional states / national coverage — `--state ALL`
- [x] Hospital website resolution via Google Places (`scripts/resolve_hospital_websites.py`)
- [ ] Enrich with additional CMS sources (Hospital General Information, NPI)
