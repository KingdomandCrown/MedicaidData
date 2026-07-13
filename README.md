# Hospitals — a public U.S. hospital knowledge base

A small, reproducible pipeline that builds a knowledge base of U.S. hospitals
from public federal data. Two data sources feed it today:

1. **CMS Provider of Services (POS) file** — the identity/location spine: every
   Medicare/Medicaid-certified provider in the country. Coverage is built out
   one state at a time (**Kansas** and **Maryland** work today; any other state
   ships by passing a different `--state`).
2. **CMS Hospital Price Transparency files** — the "standard charges" a hospital
   publishes, including full negotiated rates per payer and plan.

The POS pipeline:

1. **Downloads** the latest CMS Provider of Services file.
2. **Filters** it to active hospitals in the target state.
3. **Normalizes** identifiers (CCN, ZIP, phone) and decodes coded fields
   (provider subtype, ownership, active/terminated status).
4. **Loads** the result into SQLite (default) or a PostgreSQL-compatible
   database, recording a provenance row for every run.

The price transparency pipeline parses each hospital's machine-readable file
(both the "tall" and "wide" CMS v3.0 layouts), keys it by EIN and
organizational NPI, and loads the full item × payer × plan negotiated-rate
matrix. See [Price transparency](#price-transparency-standard-charges) below.

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

## Price transparency (standard charges)

Since 2024, hospitals must publish a machine-readable "standard charges" file.
Unlike POS, there is no single national feed — each hospital posts its own file
on its website — so ingestion is **file-driven**: download the file(s) and
point the tool at them.

```bash
# A single hospital's file (.csv or .zip)
hospitals ingest-charges /path/to/520591656_hospital_standardcharges.csv

# A whole directory of downloaded files
hospitals ingest-charges /path/to/mrf_downloads/

# Smoke-test a very large file by capping items read
hospitals ingest-charges /path/to/huge_hospital.zip --limit 1000
```

The parser handles both physical layouts of the CMS v3.0 schema automatically:

- **tall** — one row per item × payer × plan (`payer_name`/`plan_name` are
  columns).
- **wide** — one row per item with each payer/plan spread across its own set of
  columns (e.g. `standard_charge|AETNA [1003]|AETNA PPO [100307]|negotiated_dollar`).
  These are unpivoted back into item × payer × plan rows.

Parsing is header-driven (column *order* varies between hospitals), and files
are streamed — a `.zip` is read without extracting it, so a 340 MB file stays
memory- and disk-friendly. Each file is keyed by its **EIN** (from the
filename) and **organizational NPI** (from the metadata), which are the join
keys back to the POS hospitals via an NPI↔CCN crosswalk (a planned step; the
`charge_sources.ccn` column is reserved for it).

Re-ingesting the same file replaces its prior load (idempotent).

### Linking charges to POS hospitals

Price-transparency files are keyed by **NPI/EIN**; POS hospitals are keyed by
**CCN**. The MRF schema has no CCN, so the two are joined through an
**NPI → CCN crosswalk** that you supply as a CSV (columns `npi`, `ccn`, and an
optional `name`):

```bash
# Load a crosswalk and link in one step
hospitals link-charges --crosswalk /path/to/npi_ccn.csv

# Or load once, link later
hospitals load-crosswalk /path/to/npi_ccn.csv
hospitals link-charges
```

Linking fills `charge_sources.ccn` and records how each match was made in
`charge_sources.link_method`:

- **`crosswalk_npi`** — authoritative match via the NPI → CCN crosswalk.
- **`name_state`** — heuristic fallback: a hospital whose normalized name +
  state uniquely matches a POS hospital (skipped when ambiguous). Disable with
  `--no-name-fallback` to use the crosswalk only.

Once linked, charges join straight to hospitals:

```sql
SELECT h.name, s.payer_name, s.plan_name, s.negotiated_dollar, s.description
FROM standard_charges s
JOIN charge_sources cs ON s.source_id = cs.id
JOIN hospitals h       ON cs.ccn = h.ccn
WHERE s.negotiated_dollar IS NOT NULL;
```

> There is no single authoritative public NPI → CCN file; crosswalks are
> typically derived from NPPES + CMS provider datasets. The linker is source
> agnostic — bring any CSV with `npi,ccn` columns.

---

## CLI reference

```
hospitals ingest [options]
  --state STATE            USPS code or name to ingest (default: KS)
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --input-file PATH        Read a local POS CSV instead of downloading from CMS
  --include-inactive       Keep providers whose termination code is not "active"
  --limit N                Cap on raw records read (smoke testing)
  --echo-sql               Echo SQL statements (debugging)

hospitals ingest-charges PATH [options]
  PATH                     An MRF .csv/.zip, or a directory of them
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --limit N                Cap on items (data rows) read per file
  --echo-sql               Echo SQL statements (debugging)

hospitals link-charges [options]
  --crosswalk PATH         Load this NPI->CCN CSV before linking
  --no-name-fallback       Use the crosswalk only (skip the name+state heuristic)
  --database-url URL       SQLAlchemy URL

hospitals load-crosswalk PATH [--source LABEL] [--database-url URL]
  Load an NPI->CCN crosswalk CSV (columns: npi, ccn, [name]).

hospitals stats [--state STATE] [--database-url URL]
  Show hospital and standard-charge row counts in the database.

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

One row per POS ingest: source, state, edition, records read/loaded,
timestamps, and status.

### `charge_sources`

One row per ingested price transparency file. Hospital name/location/address,
`ein`, `primary_npi` (+ all `npis`), `license_number`/`license_state`,
`mrf_version`, `layout` (`tall`/`wide`), `last_updated_on`, `charge_count`, and
`ccn`/`link_method` populated by the linker to bridge to POS `hospitals`.

### `npi_ccn_crosswalk`

The NPI → CCN bridge, loaded from a CSV: `npi` (PK), `ccn`, optional `name`,
`source`, `loaded_at`.

### `standard_charges`

One row per item × payer × plan, linked to `charge_sources` via `source_id`
(and denormalized `ein`/`primary_npi` for easy joins). Columns: `description`,
`code`/`code_type` (+ `additional_codes`), `billing_class`, `setting`, drug
unit/type, `modifiers`, item-level `gross_charge`/`discounted_cash`/
`min_charge`/`max_charge`, and per-payer `payer_name`/`plan_name`,
`negotiated_dollar`/`negotiated_percentage`/`negotiated_algorithm`,
`methodology`, `median_amount`, `percentile_10`/`percentile_90`, `count`, and
`additional_notes`.

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
  cli.py                argparse CLI (ingest, ingest-charges, stats)
  ingest.py             POS orchestration: fetch -> filter -> normalize -> load
  cms_pos.py            CMS discovery + data-api / CSV / local-file fetching
  normalize.py          POS column mapping, identifier normalization, code lookups
  price_transparency.py MRF parser: tall + wide v3.0 layouts, streamed
  ingest_charges.py     charge-file orchestration (single file or directory)
  link.py               NPI->CCN crosswalk loader + charge<->hospital linker
  db.py                 SQLAlchemy schema + dialect-aware upsert (SQLite/Postgres)
  states.py             USPS / SSA / FIPS state reference data
  logging_config.py     logging setup
tests/
  fixtures/pos_sample.csv        representative POS rows (synthetic)
  fixtures/mrf_tall_sample.csv   tall-layout standard charges (synthetic)
  fixtures/mrf_wide_sample.csv   wide-layout standard charges (synthetic)
  test_normalize.py              identifier + code normalization
  test_cms_pos.py                discovery, pagination, CSV reading
  test_ingest.py                 POS end-to-end into SQLite
  test_price_transparency.py     MRF parsing (tall + wide, unpivot)
  test_ingest_charges.py         charge ingestion end-to-end into SQLite
  test_link.py                   crosswalk load + NPI/name linking
```

---

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests are hermetic: they run the full pipelines against the synthetic,
format-accurate fixtures in `tests/fixtures/` and never touch the network. The
CMS discovery and pagination logic is tested with mocked HTTP responses; the
MRF parser is tested on both tall and wide layouts including the wide unpivot.

---

## Roadmap

- [x] Kansas — CMS Provider of Services ingestion
- [x] Maryland — same pipeline, `--state MD`
- [x] Price transparency — CMS v3.0 standard charges (tall + wide), full
      negotiated rates
- [x] NPI↔CCN crosswalk + linker to join `charge_sources` to POS `hospitals`
- [ ] Enrich with additional CMS sources (Hospital General Information, NPPES)
- [ ] Additional states / national coverage
