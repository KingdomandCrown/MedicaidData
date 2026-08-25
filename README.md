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

CMS restructured this dataset in 2026. The old "Hospital & Non-Hospital
Facilities" file is gone, replaced by three named for the system that produces
them: **QIES** (hospitals — the file is literally `Hospital_and_other.DATA`),
**iQIES** (post-acute: home health, hospice, SNF, ASC) and **Clinical
Laboratories**. The pipeline defaults to the QIES file; `--dataset-title` picks
a different one. Discovery matches titles by meaning rather than by exact
string, and warns when it had to, naming every dataset that also matched.

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
point the tool at them. These files are big (tens of MB to multiple GB) and
live wherever you downloaded them, so **run the tool locally against your
download folder** rather than moving the files around:

```bash
# Ingest an entire folder of downloaded files (.csv, .zip, .json, .xlsx)
hospitals ingest-charges ~/Downloads/

# A single hospital's file
hospitals ingest-charges ~/Downloads/520591656_the-johns-hopkins-hospital_standardcharges.csv

# Smoke-test a very large file by capping items read
hospitals ingest-charges ~/Downloads/410944601_mayo-clinic_standardcharges.csv --limit 1000

# A large batch: resumable, and one bad file will not end the run
hospitals ingest-charges ~/Downloads/round5/ --skip-existing --continue-on-error
```

At batch scale two flags matter. `--skip-existing` leaves files already loaded
alone, so a run that dies at file 300 of 400 picks up where it stopped rather
than redoing everything. `--continue-on-error` logs a failing file and moves on,
listing every failure at the end instead of aborting the batch. Progress is
logged as `[n/total]` so a long run is followable.

"Already loaded" means *loaded at least one charge row*. A file that parsed but
produced nothing is left in the retry set, so re-running after a parser fix
reaches it instead of skipping it forever.

The parser handles every physical layout CMS files arrive in, auto-detected:

- **tall CSV** — one row per item × payer × plan (`payer_name`/`plan_name` are
  columns).
- **wide CSV** — one row per item with each payer/plan spread across its own set
  of columns (e.g. `standard_charge|AETNA [1003]|AETNA PPO [100307]|negotiated_dollar`).
  These are unpivoted back into item × payer × plan rows.
- **JSON** — the nested CMS JSON schema (`standard_charge_information[] ->
  standard_charges[] -> payers_information[]`), streamed with `ijson`.
- **XLSX** — the same tall/wide layouts shipped as a spreadsheet, read row by
  row with `openpyxl` in read-only mode. A spreadsheet is just another way of
  producing the same rows, so it shares the CSV layout logic exactly.
- **GZIP** — a `.csv.gz` or `.json.gz` is decompressed on the fly, so a large
  system's multi-gigabyte export never lands on disk uncompressed.

Finding the data header is a matter of **evidence, not a keyword**. A column
that could only belong to the charges (`description`, `code|1`, `payer_name`,
anything starting with `standard_charge|`) settles it; a merely suggestive one
(`setting`, `billing_class`) needs a second; and a column that names the
hospital (`hospital_name`, `version`, `license_number|XX`) disqualifies the row
outright. That last rule matters: Hartford HealthCare's *metadata* header
carries a column called `setting`, and treating one such word as proof cost six
hospitals a zero-row load.

Real-world files also vary in *where the data starts*. The CMS template puts two
metadata rows (hospital name, NPI, license, version) above the data header, but
some hospitals publish the data header on line 1 with no preamble, and others
leave a blank spacer row in between. The header block is **located by column
name** rather than assumed to be line 3, so all three arrangements load. A file
with no preamble has no hospital name or NPI to read, so it is identified by its
filename EIN alone. A file with no recognizable data header now fails loudly
instead of quietly loading zero rows.

Encoding is detected per file: many hospitals export from Excel on Windows, so
the file arrives in cp1252 rather than UTF-8, and published JSON often carries a
UTF-8 byte order mark. Both are handled without mangling genuine UTF-8 files.

Everything is **streamed** — CSVs row by row, a `.zip` read without extracting
it, and JSON parsed iteratively — so a 340 MB (or multi-GB) file stays memory-
and disk-friendly. Each file is keyed by its **EIN** (from the filename) and,
for CSV files, its **organizational NPI** (from the metadata); these are the
join keys back to the POS hospitals (see [linking](#linking-charges-to-pos-hospitals)).
Some systems name files `<ein>-<npi>_<name>`, and an upload may prepend a hex
hash. Only a prefix containing a hex *letter* is treated as a hash — a leading
run of digits is an identifier, not noise.
JSON files often omit the NPI, so they link by name + state or an EIN-based
crosswalk.

A **subdirectory** is not descended into by default — a round folder usually
sits beside its siblings — but it is named in the log, before and after the
run, with a pointer to `--recursive`. Large systems arrive as a folder of
facilities, and a silently skipped folder is a hundred missing hospitals that
look exactly like a clean run.

A file whose extension the ingester does not recognize is **named in the log**
before the run starts and counted again at the end, rather than passed over in
silence. A half-finished `.crdownload` sitting in a download folder looks
exactly like a hospital that was never ingested, and silence is how one gets
missed.

Re-ingesting the same file replaces its prior load (idempotent).

A SQLite database is opened in **WAL mode** with a 30-second busy timeout, so
`stats`, `gap-report`, and `archive-ingested` all work *while* a long ingest is
running. Without it one writer blocks every reader and a routine check fails
with "database is locked". `synchronous=NORMAL` goes with WAL: a crash can cost
the most recent commits but cannot corrupt the file, which is the right trade
for a store rebuilt from files on disk.

Only one ingest may write at a time. Starting a second one against the same
database will fail — that is SQLite, not a bug.

### Finding data stored twice

Two habits put the same rows in the store repeatedly: a folder holding both
`file.csv` and `file (1).csv`, and a large system publishing **one file per
EIN** with a copy named for each facility. HCA does the latter — thirteen
HealthOne facilities share EIN `841321373`, and their row counts repeat in
clusters because several names point at one dataset.

```bash
hospitals duplicates --database-url sqlite:///data/round5.sqlite
```

Files are grouped by EIN *and* exact row count: two files agreeing to the digit
across three million rows are the same data, not a coincidence. The report then
splits the redundancy into the two habits that cause it, because only one of
them can be fixed by deleting:

* **The same file downloaded twice** — `anmed.zip` and `anmed (1).zip`. The
  copy carries no fact the original does not.
* **One dataset published per facility** — four Cone Health hospitals shipping
  identical files. Each copy's `charge_sources` row is the only record that
  the facility exists and what it is called.

Which one applies is decided by the **filenames**, not the hospital name stored
in the file: a system that publishes per facility usually stamps its own name
in every copy, so the internal name reads "one hospital downloaded four times"
when the filenames plainly name four hospitals.

The re-downloads can then be dropped:

```bash
hospitals prune-duplicates --database-url sqlite:///data/round5.sqlite
hospitals prune-duplicates --database-url sqlite:///data/round5.sqlite --apply
```

Dry run unless `--apply`. The copy without a counter survives, deletion runs in
small transactions so an interrupt loses only the batch in flight, and re-running
it is a no-op. Per-facility copies are never touched — storing one copy with many
facilities pointing at it is a schema change, and a deliberate one.

SQLite frees the pages but does not shrink the file: the space is reused by the
next load rather than returned to the volume. `VACUUM` would return it, but needs
as much free disk as the database is large.

### Repairing EINs written by an older parser

The duplicate report is also how a parsing bug surfaces: the same Atrium file
appeared under two EINs, `166934899` and `334038167`. The first is not an EIN —
it is the organizational NPI `1669348991` with its last digit cut off, because
the filename parser matched nine digits without checking whether ten were there.
8.5 million charge rows were filed under an organization that does not exist,
where no benchmark of the real hospital can see them.

The parser now refuses to read a ten-digit NPI as an EIN and falls back to
recording it as the NPI. For rows already written:

```bash
hospitals repair-eins --database-url sqlite:///data/round5.sqlite
hospitals repair-eins --database-url sqlite:///data/round5.sqlite --apply
```

Dry run unless `--apply`, and safe to repeat — it re-derives each EIN from the
filename, so running it again after any future parser change is the way to pick
up the correction. `--sources-only` fixes the one row per file in
`charge_sources` and skips the expensive half, which rewrites `standard_charges`
for every charge row of every affected file. Prune first: there is no sense
rewriting rows that are about to be deleted.

Files repaired this way end up with no EIN at all, because their systems publish
under the NPI alone. Duplicate detection therefore keys on **EIN or NPI**,
whichever the file has — grouping on the EIN alone would stop checking Atrium
and Charlotte-Mecklenburg, the largest duplicates in the store, the moment their
invented EINs were corrected.

### Running two commands at once

One MRF can insert nine million rows in a single transaction, so a second writer
arriving mid-file waits up to five minutes for the lock. Reads are unaffected
(WAL). If the wait expires, the command says so in a line and exits 3, having
written nothing:

```
ERROR: the database is locked by another writer — an ingest is almost
certainly still running.
```

Set `HOSPITALS_SQLITE_BUSY_TIMEOUT_MS` to wait a different length of time, or a
small value to be told immediately rather than wait at all.

### Linking charges to POS hospitals

Price-transparency files are keyed by **NPI/EIN**; POS hospitals are keyed by
**CCN**. The MRF schema has no CCN, so the two are joined through an
**NPI → CCN crosswalk** that you supply as a CSV (columns `npi`, `ccn`, and an
optional `name`):

Getting that crosswalk is a command, not a chore — CMS publishes **Hospital
Enrollments**, which lists the NPI and the CCN of every enrolled hospital:

```bash
hospitals fetch-crosswalk --database-url sqlite:///data/round5.sqlite
hospitals backfill-npis  --database-url sqlite:///data/round5.sqlite --apply
hospitals link-charges   --database-url sqlite:///data/round5.sqlite
```

`backfill-npis` matters as much as the crosswalk. Many published JSON files omit
`type_2_npi` entirely while naming the NPI in the filename, so the column the
linker reads is empty and the crosswalk has nothing to match. It stores the
filename's NPI on those sources; dry run unless `--apply`.

This is what the name heuristic cannot do: the largest publishers name a
*system*. There is no hospital called Dignity Health — there are twenty — so
`941196203-1770626426_dignity-health_standardcharges.json` matches nothing by
name and exactly one hospital by NPI.

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

### Tidying the download folder

Once a batch is loaded, the finished files bury the ones still needing
attention. `archive-ingested` moves out only what the database confirms is
loaded — matched on the same filename key the ingester stored, and only where
at least one charge row landed:

```bash
# Preview (default: nothing is touched)
hospitals archive-ingested ~/Downloads/round5 --to ~/Downloads/_to_delete \
    --database-url sqlite:///data/round5.sqlite

# Do it
hospitals archive-ingested ~/Downloads/round5 --to ~/Downloads/_to_delete \
    --database-url sqlite:///data/round5.sqlite --apply
```

Files that failed to parse, that loaded zero rows, or that are not an
ingestible type stay put and are listed — so what remains in the folder is the
work left. Nothing is deleted, and a name already taken at the destination is
reported rather than overwritten.

---

## What still needs downloading

There is no national feed of price-transparency files — each hospital publishes
its own — so the download worklist is the gap between the POS universe (every
Medicare-certified hospital) and what has actually been ingested:

```bash
# 1. Load the hospital universe once (single pass over the national POS file)
hospitals ingest --state ALL --database-url sqlite:///data/round5.sqlite

# 2. Write the worklist
hospitals gap-report --database-url sqlite:///data/round5.sqlite \
    --output hospitals_to_download.xlsx
```

The workbook has three sheets:

- **Priority Systems** — candidate health systems ranked by how many hospitals
  one visit yields. Systems we have *already* parsed a file from sort to the
  top (the URL is known, and the rest are usually on the same page); the
  `Known file` column names that file.
- **Independents by State** — everything not in a system, grouped by state,
  with CCN, city, type, and bed count.
- **State Coverage** — per-state downloaded / remaining / % covered, worst
  first, with states that have nothing at all flagged `not started`.
- **Unattributed Files** — files already ingested whose hospital could not be
  identified (no metadata preamble, no EIN in the filename). They are neither
  counted as covered nor listed as gaps: counting them either way would hide a
  hospital or have someone download it twice. Fill in the `Assign CCN` column
  and load it as a crosswalk to resolve them.

A hospital counts as covered when a charge source links to its CCN *or* matches
its name and state unambiguously — so the report is usable before the NPI→CCN
crosswalk is loaded. Files that parsed but produced no rows do not count.

System membership is not in the POS file, so it is inferred from the leading
brand token in the hospital name (`BAYLOR`, `ASCENSION`, `CHRISTUS`); names that
lead with a descriptor fall through to the first real name, and `Saint`/`St`
take a second token so unrelated systems do not merge. It is a heuristic for
*ordering* the work — `--min-system-size` controls how big a cluster has to be
before it is called a system.

---

## CLI reference

```
hospitals ingest [options]
  --state STATE            USPS code or name, or ALL for every state (default: KS)
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --input-file PATH        Read a local POS CSV instead of downloading from CMS
  --include-inactive       Keep providers whose termination code is not "active"
  --limit N                Cap on raw records read (smoke testing)
  --echo-sql               Echo SQL statements (debugging)

hospitals ingest-charges PATH [options]
  PATH                     An MRF .csv/.zip/.json/.xlsx/.gz, or a directory of them
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --limit N                Cap on items (data rows) read per file
  --skip-existing          Skip files already loaded (resumable batches)
  --recursive              Descend into subdirectories (a system's facilities)
  --continue-on-error      Log and skip failing files; report them at the end
  --echo-sql               Echo SQL statements (debugging)

hospitals duplicates [options]
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --limit N                Groups to list (default: 25)

hospitals prune-duplicates [options]
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --apply                  Actually delete; without it, only report what would go
  --limit N                Files to list (default: 40)

hospitals repair-eins [options]
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --apply                  Actually rewrite; without it, only report what disagrees
  --sources-only           Fix charge_sources only; leave the charge rows for later
  --limit N                Sources to list (default: 40)

hospitals price CODE [options]
  CODE                     A billing code, e.g. 85025 (CBC with differential)
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --state ST               Restrict to hospitals in one state
  --payer NAME             Restrict to payers matching this text
  --include-unlinked       Include files not attributed to a hospital
  --limit N                Payers to list (default: 15)

hospitals coverage PATTERN [options]
  PATTERN                  Part of a hospital, system, or file name
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --state ST               Restrict matching hospitals to one state
  --limit N                Rows to list per section (default: 25)

hospitals scan-ingested [PATH ...] [options]
  PATH ...                 Folders to walk (default: ~/Desktop ~/Downloads ~/Documents)
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --output PATH            Write the already-ingested paths, one per line
  --limit N                Folders to list (default: 20)

hospitals archive-ingested PATH --to DEST [options]
  PATH                     Folder of MRF files to tidy
  --to DEST                Folder to move loaded files into (created if missing)
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --apply                  Actually move; without it, only report what would move
  --recursive              Descend into subdirectories, mirroring them at DEST

hospitals gap-report [options]
  --database-url URL       SQLAlchemy URL (default: sqlite:///data/hospitals.sqlite)
  --output PATH            Workbook to write (default: hospitals_to_download.xlsx)
  --min-system-size N      Cluster size before it counts as a system (default: 2)

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
`mrf_version`, `layout` (`tall`/`wide`/`json`), `last_updated_on`,
`charge_count`, and `ccn`/`link_method` populated by the linker to bridge to
POS `hospitals`.

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
  price_transparency.py MRF parser: tall + wide CSV and JSON layouts, streamed
  ingest_charges.py     charge-file orchestration (single file or directory)
  link.py               NPI->CCN crosswalk loader + charge<->hospital linker
  gap.py                download worklist: coverage gap, system ranking, .xlsx
  archive.py            move loaded files out of the working folder
  duplicates.py         report datasets stored more than once
  db.py                 SQLAlchemy schema + dialect-aware upsert (SQLite/Postgres)
  states.py             USPS / SSA / FIPS state reference data
  logging_config.py     logging setup
tests/
  fixtures/pos_sample.csv        representative POS rows (synthetic)
  fixtures/mrf_tall_sample.csv   tall-layout standard charges (synthetic)
  fixtures/mrf_wide_sample.csv   wide-layout standard charges (synthetic)
  fixtures/mrf_sample.json       JSON-layout standard charges (synthetic)
  fixtures/mrf_tall_sample.xlsx  tall layout as a spreadsheet (synthetic)
  fixtures/mrf_headeronly_sample.csv  data header on line 1, no preamble
  fixtures/mrf_spacer_sample.csv      blank row between preamble and header
  fixtures/mrf_metadata_setting_sample.csv  metadata header sharing a data column name
  fixtures/mrf_cp1252_sample.csv      Windows-encoded CSV (+ .zip twin)
  fixtures/mrf_bom_sample.json        JSON with a UTF-8 byte order mark
  test_normalize.py              identifier + code normalization
  test_cms_pos.py                discovery, pagination, CSV reading
  test_ingest.py                 POS end-to-end into SQLite
  test_price_transparency.py     MRF parsing (tall + wide CSV, unpivot)
  test_price_transparency_json.py JSON MRF parsing + end-to-end
  test_price_transparency_xlsx.py XLSX parsing, CSV parity, mixed-folder ingest
  test_price_transparency_header.py header-block location, missing preamble
  test_price_transparency_gzip.py  gzipped MRFs, and reporting skipped files
  test_price_transparency_encoding.py cp1252 CSVs and BOM-prefixed JSON
  test_ingest_charges.py         charge ingestion end-to-end into SQLite
  test_link.py                   crosswalk load + NPI/name linking
  test_gap.py                    brand inference, coverage gap, workbook
  test_archive.py                folder tidying, dry run, collision safety
  test_duplicates.py             duplicate-load detection and its limits
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
- [x] Price transparency — CMS standard charges (tall + wide CSV, **JSON**, and
      **XLSX**), full negotiated rates, streamed
- [x] NPI↔CCN crosswalk + linker to join `charge_sources` to POS `hospitals`
- [ ] Enrich with additional CMS sources (Hospital General Information, NPPES)
- [ ] Additional states / national coverage
