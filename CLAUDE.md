# CLAUDE.md — OST Citizen Match Project

Context for Claude Code. Read this before touching anything.

---

## What this project does

Matches Kelmar unclaimed-property records against Oklahoma citizen records (SOK) to produce a confidence-bucketed review file for Treasury. Output tells Treasury which citizen most likely owns each unclaimed property, with cash value and deceased-flag enrichment.

---

## Environment

- **Cloud:** GCP
- **Project (default):** `aw-ost-property-np` (overridable via `PROJECT_ID` env var)
- **Region:** `us-central1`
- **Datasets** (all in the same project):
  - `citizen_staging` — raw inbound tables from MoveIt
  - `citizen_match` — working tables (tokenized, cleaned, match candidates, review)
  - `citizen_mpi_result` — final dated MPI output delivered to Kelmar
- **Compute:** Vertex AI Jupyter (dev), Cloud Run + Composer + Dataproc (prod)
- **Tokenization:** Cloud DLP deterministic deidentify template
- **Fuzzy lib:** `rapidfuzz` (`fuzz.WRatio`)

---

## Repo layout

```
.
├── citizenmatch/                  # Python package — runs inside Cloud Run
│   ├── config.py                  # Single source of truth: project, datasets, tables, thresholds
│   ├── cleaners.py                # Name/address/state/zip/street normalizers (shared by Kelmar + SOK)
│   ├── pipeline.py                # Pipeline stages: tokenize → clean → deterministic → fuzzy → combine → cap → deliver
│   ├── main.py                    # Cloud Run HTTP entry point — invokes pipeline.py
│   ├── test_cleaners.py           # Unit tests for cleaners.py
│   ├── Dockerfile                 # Cloud Run container build
│   └── requirements.txt           # Python deps for the container
├── composer/                      # Airflow DAGs running in Cloud Composer
│   ├── composer_cloud_run.py      # DAG `citizenmatch_trigger` — POSTs to Cloud Run, blocking
│   └── composer_dataproc.py       # DAG — Dataproc cluster lifecycle for heavier load step
├── docs/
│   └── code_review.md
├── notebooks/                     # Vertex AI dev notebooks (exploratory; not deployed)
├── README.md
└── CLAUDE.md                      # This file
```

**Working rules tied to layout:**
- Anything in `citizenmatch/` runs inside the Cloud Run container. Keep it import-clean and free of notebook idioms (no `!pip install`, no `display()`, no shell magic).
- **Whenever `cleaners.py` changes, run `test_cleaners.py`** before considering the change done. Cleaner drift silently kills match rates.
- New constants, table names, or thresholds go in `config.py`. Never inline.
- New pipeline stages go in `pipeline.py` and are called from `main.py`. Don't add HTTP handling logic outside `main.py`.
- Notebooks under `notebooks/` are exploration-only — do not import from them, and do not treat their logic as canonical. The package code in `citizenmatch/` is the source of truth.
- DAGs in `composer/` should not contain business logic. They orchestrate; the work lives in Cloud Run / Dataproc.

---

## Hard security rules — DO NOT VIOLATE

1. **Never read, print, log, or persist raw SSNs.** Only `ssn_token` (DLP output) and `ssn_last4` are allowed downstream.
2. **Never write raw PII into notebook cell outputs.** Clear outputs before commit.
3. **Never hardcode credentials, project IDs, or DLP template resource names in source files.** Always import from `citizenmatch/config.py`.
4. **Never echo file contents that may contain SSN columns** (e.g. `head` on raw staging exports). To inspect format, use a helper that returns metadata only (length, has_dash, all_digits) — never the value.
5. **If you encounter raw PII in any file, stop and flag it.** Do not "work around" it.

---

## `citizenmatch/config.py` — single source of truth

All table names, project IDs, thresholds, and tuning constants live here. **Never re-declare these in new code; import them.**

**Identity / location**
- `PROJECT_ID` — default `aw-ost-property-np`, env-overridable
- `LOCATION` — default `us-central1`
- `IMPERSONATE_SA` — optional service account for impersonation; empty string means no impersonation

**Datasets**
- `STAGING_DATASET = "citizen_staging"`
- `OUTPUT_DATASET = "citizen_match"`
- `MPI_DATASET = "citizen_mpi_result"`

**DLP**
- `DLP_TEMPLATE_NAME` — built from `PROJECT_ID` + `LOCATION` + a fixed template ID. Updating the project automatically targets the right template path **only if a template with that ID exists in the new project.** Verify before assuming.
- `DLP_BATCH_SIZE = 200`
- `MAX_RETRIES = 6`

**BigQuery batching**
- `BQ_CHUNK_ROWS = 500` — much smaller than the dev notebook's 100,000. Production keeps memory low.

**Test-mode footgun (READ THIS)**
- `SOK_MAX_ROWS = 500` by default — limits SOK rows processed for fast iteration.
- **A full run requires `SOK_MAX_ROWS=0`** (or any value larger than the table). Running with defaults will silently process only 500 SOK rows.

**Tables (working dataset `citizen_match`)**
- Staging in: `SOK_STAGING = citizen_staging.boost_staging`, `KELMAR_STAGING = citizen_staging.OK_OST_OMES_DataMatch`
- Tokenized: `KELMAR_TOKENIZED`, `SOK_TOKENIZED`
- Cleaned: `KELMAR_CLEAN`, `SOK_CLEAN`, `SOK_NULL_CLEAN` (separate table for SSN-null SOK rows)
- Fuzzy intermediates: `KELMAR_UNMATCHED`, `KELMAR_FUZZY`, `FUZZY_POOL`, `BLOCK1_CANDIDATES`, `BLOCK2_CANDIDATES`, `BLOCK1_CLASSIFIED`, `BLOCK2_CLASSIFIED`
- Deterministic: `DET_MATCHES`
- Review tables: `REVIEW_TABLE` (v2), `REVIEW_ENRICHED` (v3), `REVIEW_CAPPED_V1`, `REVIEW_CAPPED_V2`, `UNMATCHED_TABLE`

**Final delivery (dataset `citizen_mpi_result`)**
- `MPI_OUTPUT_PREFIX = ...citizen_mpi_result.OK_OST_OMES_Output_DataMatch` — **a date suffix is appended at runtime.** Do not write to the prefix directly.

**Tuning constants — use these, do not redefine**
- `BLOCK1_CONFIDENCE_CAP = 95`
- `BLOCK2_CONFIDENCE_CAP = 85`
- `AUTO_APPROVE_THRESHOLD = 90`
- `REVIEW_THRESHOLD = 80`
- `NAME_WEIGHT = 0.6`
- `STREET_WEIGHT = 0.4`

---

## Pipeline stages

### 1. Tokenize
- DLP deterministic template → same SSN always produces the same token.
- Source: `SOK_STAGING`, `KELMAR_STAGING`. Output: `SOK_TOKENIZED`, `KELMAR_TOKENIZED`.
- Raw `SSN` column is **dropped** before the tokenized table is written.
- Batch size = `DLP_BATCH_SIZE`, exponential backoff on 429 / `ResourceExhausted`.

### 2. Clean
- Identical cleaners on Kelmar, SOK, and SOK-null paths.
- **Names** (`clean_name`): accent-strip → upper → drop titles (`MR`, `MRS`, `DR`, `PROF`, `REV`, `JUDGE`, etc.) → strip non-name punctuation → collapse whitespace.
- **Street** (`clean_street`): accent-strip → upper → normalize PO Box variants (`P.O. BOX`, `PO Box` → `PO BOX`) → collapse `APARTMENT`/`APT` to `APT` and `SUITE`/`STE` to `STE` → rewrite `#X` as `APT X` → standardize directionals (`NORTH` → `N`) and street types (`STREET` → `ST`) → strip remaining punctuation → collapse whitespace. **Titles are NOT dropped from streets** — street names commonly contain title-shaped tokens (`DR MARTIN LUTHER KING BLVD`, `JUDGE HARRIS PKWY`) and stripping them would corrupt the address.
- **State** (`clean_state`): accent-strip → upper → keep first 2 letters only.
- **Zip** (`clean_zip`): keep first 5 digits only.
- Outputs: `KELMAR_CLEAN`, `SOK_CLEAN`, `SOK_NULL_CLEAN`.

### 3. Deterministic match
- `JOIN ON k.ssn_token = s.ssn_token`.
- Output: `DET_MATCHES`. **No `bucket` or `match_flag` column on this table** — those are assigned in Stage 5.
- Confidence is computed as `100 * composite_score / 100` for deterministic rows.

### 4. Fuzzy match
Built only against Kelmar rows that did not match deterministically (`KELMAR_UNMATCHED` → `KELMAR_FUZZY`).
SOK candidate pool = `SOK_NULL_CLEAN` ∪ (SSN-present clean rows not in `DET_MATCHES`) → `FUZZY_POOL`.

**Block 1 — ZIP + Last + DOB** (output `BLOCK1_CLASSIFIED`)
- `confidence_score = BLOCK1_CONFIDENCE_CAP * composite_score / 100`

**Block 2 — ZIP + Last only** (output `BLOCK2_CLASSIFIED`)
- `confidence_score = BLOCK2_CONFIDENCE_CAP * composite_score / 100`
- **Cannot produce `FUZZY_AUTO_APPROVE` by design** — cap (85) < auto-approve threshold (90).

**Composite score (both blocks):**
```
composite_score = NAME_WEIGHT * name_score + STREET_WEIGHT * street_score
```
where `name_score` and `street_score` are `rapidfuzz.fuzz.WRatio(...)`.

### 5. Combine + cap + enrich + bucket assignment
- Union deterministic + Block 1 + Block 2 → `REVIEW_TABLE`.
- **Bucket and `match_flag` are assigned here, in one place, for every row.** See the bucket logic block below.
- Enrich with `CashValue` (Kelmar) and `Deceased` (SOK staging) → `REVIEW_ENRICHED`.
- Cap to one candidate per `(OwnerID, PropertyID)` using `ROW_NUMBER()` ordered by `confidence_score DESC` → `REVIEW_CAPPED_V2`. Deterministic rows are always kept regardless of rank.
- Add `Eligibility_Flag` column (see Eligibility section below).
- Final delivery: dated table under `MPI_OUTPUT_PREFIX` in `citizen_mpi_result`. Scores rounded to 1 decimal.

---

## Bucket assignment logic (single source of truth)

Every match is assigned a confidence bucket that determines its workflow: auto-approve, review, or reject. **This logic runs exactly once, in the combine step (Stage 5), against the unioned `REVIEW_TABLE`.** No intermediate table (`DET_MATCHES`, `BLOCK1_CLASSIFIED`, `BLOCK2_CLASSIFIED`) carries a competing `bucket` column.

**Deterministic SSN matches.** The SSN confirms identity, so the bucket depends on whether the names also agree (since the SSN is the same on both sides):

| Name agreement | bucket | match_flag |
|---|---|---|
| Exact match (case/whitespace ignored) | `DET_AUTO_APPROVE` | `NULL` |
| `name_score >= 90` | `DET_REVIEW_MINOR` | `SSN_MATCH_NAME_MISMATCH` |
| `name_score >= 80` | `DET_REVIEW_MODERATE` | `SSN_MATCH_NAME_MISMATCH` |
| `name_score < 80` | `DET_REVIEW_MISMATCH` | `SSN_CONFLICT_DIFFERENT_IDENTITY` |

The 3-state `match_flag` lets Treasury filter "slight name variation" vs. "completely different identity that happens to share an SSN token" (an upstream data-quality signal, not a pipeline bug).

**Fuzzy matches.** No SSN confirmation, so the bucket is driven by `confidence_score`:

| Condition | bucket |
|---|---|
| `confidence_score >= 90` | `FUZZY_AUTO_APPROVE` |
| `confidence_score >= 80` | `FUZZY_REVIEW` |
| `confidence_score < 80` | `FUZZY_REJECT` |

Fuzzy rows leave `match_flag = NULL`.

**Implementation rules:**
- Use `AUTO_APPROVE_THRESHOLD` (90) and `REVIEW_THRESHOLD` (80) from `config.py`. Never hardcode.
- Block 2 cannot reach `FUZZY_AUTO_APPROVE` because its confidence cap (85) is below the auto-approve threshold (90). This is by design.
- After assignment, `assert combined["bucket"].isna().sum() == 0` before writing to BigQuery.

---

## Eligibility flag (delivered to Kelmar)

`Eligibility_Flag` is a delivered column on the final MPI output. It tells Treasury / Kelmar whether a matched citizen is eligible to receive their unclaimed property.

| Condition | Eligibility_Flag |
|---|---|
| `Deceased = TRUE` | `INELIGIBLE_DECEASED` |
| Otherwise | `ELIGIBLE` |

Set in Stage 5 alongside the cap step (when `REVIEW_CAPPED_V2` is built).

**Today this is the only ineligibility reason.** If new rules are added later (minors, missing fields, etc.), add them as additional `INELIGIBLE_<REASON>` values — keep the `ELIGIBLE` / `INELIGIBLE_*` naming convention so existing Treasury filters keep working.

---

## Schemas (key columns)

**Kelmar (`KELMAR_CLEAN`):** `OwnerID`, `PropertyID`, `NameLast`, `NameFirst`, `NameMiddle`, `Address1/2/3`, `City`, `State`, `Zip`, `BirthDT`, `CashValue`, `ssn_token`, `ssn_last4`, plus cleaned: `first_name_clean`, `middle_name_clean`, `last_name_clean`, `full_name_clean`, `street_clean`, `city_clean`, `state_clean`, `zip_clean`.

**SOK (`SOK_CLEAN`, `SOK_NULL_CLEAN`):** `DLN`, `Transaction_ID`, `_data_file_date_`, `Transaction_Date`, `Transaction_Type`, `First_Name`, `Middle_Name`, `Last_Name`, `Suffix`, `Date_of_Birth`, `Residential_Address_*`, `Mailing_Address_*`, `ssn_token` (null in null-clean table), plus the same `*_clean` fields as Kelmar. Note: these schemas come from the dev notebook against the old staging tables — **verify column names against `citizen_staging.boost_staging` and `citizen_staging.OK_OST_OMES_DataMatch` before writing new code; the new staging tables may differ.**

**Final review (`REVIEW_ENRICHED` / `REVIEW_CAPPED_V2`):** `OwnerID`, `PropertyID`, `DLN`, `Transaction_ID`, `_data_file_date_`, `kelmar_name/street/city/state/zip`, `BirthDT`, `sok_name/street/city/state/zip`, `Date_of_Birth`, `technique`, `name_score`, `street_score`, `composite_score`, `confidence_score`, `bucket`, `match_flag`, `CashValue`, `Deceased`, `Eligibility_Flag` (added in `REVIEW_CAPPED_V2`).

---

## Known pitfalls (already paid for in debug time — don't reintroduce)

1. **Silent null buckets from `.map()` rename.** A previous version remapped fuzzy bucket labels using unprefixed keys against already-prefixed values, silently nulling all fuzzy buckets. **Always assert `combined["bucket"].isna().sum() == 0` before writing to BigQuery.**
2. **SOK fan-out on DLN joins.** SOK has multiple historical rows per DLN. Joining directly multiplies rows. Fix: `ROW_NUMBER() OVER (PARTITION BY DLN ORDER BY _data_file_date_ DESC NULLS LAST) = 1`. For fuzzy validation, union with `SOK_NULL_CLEAN` since fuzzy can pull from there.
3. **Capping must partition on `(OwnerID, PropertyID)`, not just `OwnerID`.** Each property gets its own best candidate. Cap is `= 1`.
4. **`DET_AUTO_APPROVE` can show lower confidence than `DET_REVIEW_*` rows.** Confidence includes street score; bucket assignment does not. Expected, not a bug.
5. **Block 2 will never auto-approve.** Confidence cap of 85 < threshold of 90. By design.
6. **`Transaction_ID` can be NULL.** Use `COALESCE(CAST(Transaction_ID AS STRING), '')` in keyset pagination so rows are not skipped.
7. **`SOK_MAX_ROWS = 500` test default.** A "successful" run in dev may only have processed 500 rows. Always confirm `SOK_MAX_ROWS` before claiming a full run.

---

## Mandatory guardrails for any code change

- Cleaners must remain **identical** between Kelmar, SOK, and SOK-null paths. If you change one, change all three. Drift here causes silent match loss.
- **All BigQuery writes go through `_write_to_bq` in `pipeline.py`.** Direct `to_gbq` / `load_table_from_dataframe` calls are not allowed in new code. The helper centralizes dtype protection and the `null_buckets == 0` assertion — bypassing it means the next dtype fix or schema guard won't apply to your write site.
- **Bucket and `match_flag` are assigned in exactly one place** — the combine step in `assemble_review_table`. No intermediate table (`DET_MATCHES`, `BLOCK1_CLASSIFIED`, `BLOCK2_CLASSIFIED`) carries a `bucket` column that competes with the final assignment. See the "Bucket assignment logic" section for the full rule.
- Any new BigQuery write must `assert null_buckets == 0` (or equivalent for the column being validated).
- Any new SOK join on DLN must use the latest-row pattern (`ROW_NUMBER() ... = 1`) unless you explicitly want full history.
- Any DLP call must use `DLP_TEMPLATE_NAME` from config — never hardcode a template resource name.
- Any threshold or weight must be imported from config — never redefined inline.
- Output table writes use `WRITE_TRUNCATE` only on versioned names (e.g. `_v3`). Never overwrite a delivered MPI output table.
- New code must be project-portable: read `PROJECT_ID` from config (which reads env), don't hardcode `aw-ost-property-np` or the old dev project.

---

## Productionization architecture (whiteboard)

```
OMES side:                                          OST side:
  Composer DAG 1 (read GCS -> BQ staging)             MoveIt -> Storage
       |                                                 ^
       v                                                 |
  citizen_staging tables                                 |
       |                                                 |
       v                                                 |
  Cloud Run (cleaning + matching pipeline)               |
       |                                                 |
       v                                                 |
  citizen_match working tables                           |
       |                                                 |
       v                                                 |
  citizen_mpi_result.OK_OST_OMES_Output_DataMatch_<date> |
       |                                                 |
       v                                                 |
  Composer DAG 2 (BQ -> GCS) -----------> MoveIt --------+
```

- **Cloud Run deploy:** `gcloud functions deploy ... --gen2 --runtime python310 --no-allow-unauthenticated --timeout 3600s --memory 1gi`.
- **Cloud Run URL:** `https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app`
- **Composer DAG 1** (`composer_cloud_run.py`, dag_id `citizenmatch_trigger`):
  - Single `PythonOperator` task.
  - Fetches an OIDC token via `id_token.fetch_id_token`, POSTs to Cloud Run with `Authorization: Bearer <token>`.
  - **Blocking call** — waits up to 1 hour for the pipeline to finish (`requests.post(..., timeout=3600)`), task `execution_timeout` is 2 hours.
  - Non-200 response raises `RuntimeError` and fails the task.
  - `schedule_interval=None` (manual trigger). Monthly target is `"0 7 1 * *"` — flip when ready.
  - No audit table, no sensor — the DAG trusts Cloud Run's HTTP response.
- **Composer DAG 2** (`composer_dataproc.py`): Dataproc cluster create → PySpark job → cluster delete. Used for the heavier load step.
- Features still to add: structured logging, archive table with batch numbers, feedback channel back to Kelmar.

---

## Coding preferences

- One task at a time. Validate before moving on.
- Prefer complete, copy-paste-ready scripts over diffs.
- Ad hoc SQL → write it for the BigQuery console, not wrapped in Python.
- Keep written summaries tight and single-paragraph where possible.
