# CLAUDE.md — OST Citizen Match Project

Context for Claude Code. Read this before touching anything.

---

## What this project does

Matches ~5,000 Kelmar unclaimed-property records against ~6.5M Oklahoma citizen records (SOK/BOOST) to produce a confidence-bucketed review file for Treasury. Output tells Treasury which citizen most likely owns each unclaimed property, with cash value and deceased-flag enrichment.

---

## Environment

- **Cloud:** GCP
- **Project (default):** `aw-ost-property-np` (overridable via `PROJECT_ID` env var)
- **Region / dataset location:** `us-central1`
- **Datasets** (all in the same project, all in `us-central1`):
  - `citizen_staging` — raw inbound tables (Kelmar from DE's DAG, SOK live view)
  - `citizen_match` — working tables (tokenized, cleaned, match candidates, review, checkpoint)
  - `citizen_mpi_result` — final MPI output delivered to Kelmar
- **Compute:** Cloud Run (production pipeline), Cloud Composer (orchestration)
- **Tokenization:** Cloud DLP deterministic deidentify template (template ID `4648784086040580254`)
- **KMS:** Keyring `citizenmatch-keyring`, key `citizenmatch-dlp-key` (us-central1)
- **Fuzzy lib:** `rapidfuzz` (`fuzz.WRatio`)
- **Service account:** `svc-omes-data-property-np@aw-ost-property-np.iam.gserviceaccount.com`
- **Composer environment:** `composer-ost-dev` (us-central1)

---

## Repo layout

```
.
├── citizenmatch/                  # Python package — runs inside Cloud Run
│   ├── config.py                  # Single source of truth: project, datasets, tables, thresholds
│   ├── cleaners.py                # Python name/address normalizers (used for local tests + fuzzy scoring)
│   ├── bq_udfs.py                 # BigQuery JavaScript UDFs for cleaning (production — zero memory)
│   ├── pipeline.py                # Pipeline stages: tokenize → clean → deterministic → fuzzy → combine → cap → deliver
│   ├── main.py                    # Cloud Run HTTP entry point — invokes pipeline.py
│   ├── test_cleaners.py           # Unit tests for cleaners.py
│   ├── Dockerfile                 # Cloud Run container build (gunicorn, 1hr timeout, single worker)
│   └── requirements.txt           # Python deps (includes google-cloud-storage for GCS export)
├── composer/                      # Airflow DAGs running in Cloud Composer
│   ├── composer_cloud_run.py      # DAG `citizenmatch_pipeline` — orchestrates full pipeline
│   └── composer_dataproc.py       # DAG — Dataproc cluster lifecycle (legacy, not actively used)
├── notebooks/                     # Vertex AI dev notebooks (exploratory; not deployed)
├── README.md
└── CLAUDE.md                      # This file
```

**Working rules tied to layout:**
- Anything in `citizenmatch/` runs inside the Cloud Run container. Keep it import-clean and free of notebook idioms.
- **Production cleaning uses `bq_udfs.py`** (BigQuery JavaScript UDFs). `cleaners.py` is kept for local tests and for the `similarity()` function used in fuzzy scoring. The JS UDFs must produce identical output to the Python cleaners — any drift silently kills match rates.
- New constants, table names, or thresholds go in `config.py`. Never inline.
- New pipeline stages go in `pipeline.py` and are called from `main.py`. Don't add HTTP handling logic outside `main.py`.
- Notebooks under `notebooks/` are exploration-only — do not import from them.
- DAGs in `composer/` should not contain business logic. They orchestrate; the work lives in Cloud Run.

---

## Source data

**Kelmar:** `aw-ost-property-np.citizen_staging.OK_OST_OMES_DataMatch`
- ~5,000 rows per batch. Truncated and reloaded each month by the DE's DAG (`gcs_to_bq_unclaimed_property_ingestion`).
- `BirthDT` is STRING in `MM/DD/YYYY` format.
- `OwnerID` and `PropertyID` are INT64.

**SOK:** `aw-ost-property-np.citizen_staging.boost_staging`
- Live view from `aw-fti-d360-p.boost_dailyload_prod.ab_sok_boost`. ~6.65M rows total.
- ~5.89M rows have SSN (tokenized), ~766K have SSN NULL (fuzzy only).
- `Date_of_Birth` is DATE type.
- `_data_file_date_` is DATE — set when data is uploaded to GCP (not a transaction date). Used as the incremental marker for SOK tokenization.
- `Transaction_ID` is NULL for ~96% of rows. This is source data, not a bug.
- Distribution: 72 distinct `_data_file_date_` values, largest single date has 4M rows.

---

## Hard security rules — DO NOT VIOLATE

1. **Never read, print, log, or persist raw SSNs.** Only `ssn_token` (DLP output) and `ssn_last4` are allowed downstream.
2. **Never include DLN in MPI output.** DLN is PII. The `publish_mpi_output` function uses `SELECT * EXCEPT(DLN)`.
3. **Never write raw PII into notebook cell outputs.** Clear outputs before commit.
4. **Never hardcode credentials, project IDs, or DLP template resource names in source files.** Always import from `citizenmatch/config.py`.
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
- `DLP_TEMPLATE_NAME` — built from `PROJECT_ID` + `LOCATION` + template ID `4648784086040580254`
- `DLP_BATCH_SIZE = 200`
- `MAX_RETRIES = 6`

**BigQuery batching**
- `BQ_CHUNK_ROWS = 100_000` — rows fetched per keyset pagination chunk during SOK tokenization

**SOK tokenization limit**
- `SOK_MAX_ROWS = 1_000_000` by default — limits SOK rows processed per Cloud Run trigger
- This is NOT a total limit — the Composer DAG loops automatically until all rows are processed
- 1M rows ≈ 12 minutes of DLP processing, fits within Cloud Run's 1-hour timeout
- Set to 0 via env var to remove limit (only if running outside Cloud Run)

**Tables (working dataset `citizen_match`)**
- Staging in: `SOK_STAGING = citizen_staging.boost_staging`, `KELMAR_STAGING = citizen_staging.OK_OST_OMES_DataMatch`
- Tokenized: `KELMAR_TOKENIZED`, `SOK_TOKENIZED`
- Cleaned: `KELMAR_CLEAN`, `SOK_CLEAN`, `SOK_NULL_CLEAN`
- Fuzzy intermediates: `KELMAR_UNMATCHED`, `KELMAR_FUZZY`, `FUZZY_POOL`, `BLOCK1_CANDIDATES`, `BLOCK2_CANDIDATES`, `BLOCK1_CLASSIFIED`, `BLOCK2_CLASSIFIED`
- Deterministic: `DET_MATCHES`
- Review tables: `REVIEW_TABLE`, `REVIEW_ENRICHED`, `REVIEW_CAPPED_V1`, `REVIEW_CAPPED_V2`, `UNMATCHED_TABLE`
- Checkpoint: `sok_tokenization_checkpoint` (temporary, deleted when initial load completes)

**Final delivery**
- `MPI_OUTPUT_TABLE = ...citizen_mpi_result.OK_OST_OMES_OUTBOUND_DataMatch` — overwritten each run, DLN excluded
- `GCS_MPI_BUCKET = "kelmar_outbound_files"` — GCS bucket for dated copies
- `GCS_MPI_PATH = "kelmar_mpi_files"` — folder inside bucket
- GCS filename: `OK_OST_OMES_OUTBOUND_DataMatch_MDDYY.csv` — new file each run, never overwritten. Date is when the **pipeline runs** (`datetime.utcnow()`), not when the data was loaded.

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

**Kelmar:** All rows tokenized in one batch (small table, ~5K rows).

**SOK:** Two modes, handled automatically:

**Mode 1 — INITIAL LOAD** (checkpoint exists OR no tokenized data):
- Keyset pagination through all SSN-present rows, ordered by `(Transaction_ID, DLN, _data_file_date_, SSN)`
- Processes up to `SOK_MAX_ROWS` (1M) per Cloud Run trigger
- Saves cursor position to `sok_tokenization_checkpoint` table after every 100K chunk
- When all rows processed → deletes checkpoint
- Composer DAG loops automatically: calls Cloud Run → checks checkpoint → calls again

**Mode 2 — INCREMENTAL** (no checkpoint + tokenized data exists):
- Finds `_data_file_date_` values in source NOT in tokenized table
- Processes each new date as a batch
- Used for monthly runs after initial load is complete

Raw `SSN` column is **dropped** before writing to tokenized table. Exponential backoff on 429 / `ResourceExhausted`.

### 2. Clean (BigQuery SQL — zero memory)

Production cleaning runs entirely in BigQuery via JavaScript UDFs (`bq_udfs.py`). **No data moves to Cloud Run memory.**

UDFs are created/updated at the start of each run (idempotent) in the `citizen_match` dataset:
- `clean_name(val STRING)` → accent-strip → upper → drop titles → strip punctuation → collapse whitespace
- `clean_street(val1 STRING, val2 STRING, val3 STRING)` → PO BOX normalize → APT/STE → directionals → street types → collapse whitespace
- `clean_state(val STRING)` → first 2 uppercase letters
- `clean_zip(val STRING)` → first 5 digits

Three SQL `CREATE TABLE AS SELECT` statements clean Kelmar, SOK (SSN-present), and SOK (SSN-null) with zero memory footprint.

### 3. Deterministic match
- `JOIN ON k.ssn_token = s.ssn_token` (SOK deduped by DLN via `ROW_NUMBER()`)
- Output: `DET_MATCHES`

### 4. Fuzzy match
Built only against Kelmar rows that did not match deterministically.
SOK candidate pool = `SOK_NULL_CLEAN` ∪ (SSN-present clean rows not in `DET_MATCHES`) → `FUZZY_POOL`.

**Block 1 — ZIP + Last + DOB:**
- Join condition uses `SAFE.PARSE_DATE('%m/%d/%Y', k.BirthDT) = s.Date_of_Birth` (Kelmar BirthDT is STRING `MM/DD/YYYY`, SOK Date_of_Birth is DATE)
- `confidence_score = 95 * composite_score / 100`
- CAN produce `FUZZY_AUTO_APPROVE`

**Block 2 — ZIP + Last only:**
- `confidence_score = 85 * composite_score / 100`
- **Cannot produce `FUZZY_AUTO_APPROVE` by design** — cap (85) < threshold (90)

**Composite score:** `NAME_WEIGHT * name_score + STREET_WEIGHT * street_score` where scores are `rapidfuzz.fuzz.WRatio(...)`.

### 5. Combine + cap + enrich + bucket assignment
- Union deterministic + Block 1 + Block 2 → `REVIEW_TABLE`
- Bucket and `match_flag` assigned in one place (see Bucket Logic section)
- Enrich with `CashValue` (Kelmar) and `Deceased` (SOK staging) → `REVIEW_ENRICHED`
- Cap to one fuzzy candidate per `(OwnerID, PropertyID)` via `ROW_NUMBER()`. Deterministic rows always kept.
- Add `Eligibility_Flag` → `REVIEW_CAPPED_V2`

### 6. Publish MPI output
- **BigQuery:** `citizen_mpi_result.OK_OST_OMES_OUTBOUND_DataMatch` — overwritten each run, DLN excluded, scores rounded to 1 decimal
- **GCS:** `gs://kelmar_outbound_files/kelmar_mpi_files/OK_OST_OMES_OUTBOUND_DataMatch_MDDYY.csv` — permanent dated copy, uploaded as single CSV via `google.cloud.storage`

---

## Bucket assignment logic (single source of truth)

**This logic runs exactly once, in the combine step (Stage 5).** No intermediate table carries a competing `bucket` column.

**Deterministic SSN matches:**

| Name agreement | bucket | match_flag |
|---|---|---|
| Exact match (case/whitespace ignored) | `DET_AUTO_APPROVE` | `NULL` |
| `name_score >= 90` | `DET_REVIEW_MINOR` | `SSN_MATCH_NAME_MISMATCH` |
| `name_score >= 80` | `DET_REVIEW_MODERATE` | `SSN_MATCH_NAME_MISMATCH` |
| `name_score < 80` | `DET_REVIEW_MISMATCH` | `SSN_CONFLICT_DIFFERENT_IDENTITY` |

**Fuzzy matches:**

| Condition | bucket |
|---|---|
| `confidence_score >= 90` | `FUZZY_AUTO_APPROVE` |
| `confidence_score >= 80` | `FUZZY_REVIEW` |
| `confidence_score < 80` | `FUZZY_REJECT` |

---

## Eligibility flag

| Condition | Eligibility_Flag |
|---|---|
| `Deceased = TRUE` | `INELIGIBLE_DECEASED` |
| Otherwise | `ELIGIBLE` |

---

## Production architecture

```
Monthly schedule (or manual ▶)
    │
    ├─ Task 1: trigger_kelmar_ingestion
    │   └─ Triggers DE's DAG (gcs_to_bq_unclaimed_property_ingestion)
    │   └─ DE picks latest Kelmar CSV from GCS → truncates & loads OK_OST_OMES_DataMatch
    │
    ├─ Task 2: tokenize_and_match (auto-loop)
    │   ├─ Call Cloud Run → tokenize Kelmar + 1M SOK → checkpoint saved → 200 OK
    │   │   └─ Check checkpoint → exists → call again
    │   ├─ Call Cloud Run → resume → 1M more SOK → checkpoint saved
    │   │   └─ Check checkpoint → exists → call again
    │   ├─ ...
    │   └─ Call Cloud Run → finishes SOK → deletes checkpoint → clean → match → output
    │       └─ Check checkpoint → gone, output exists → ✅ DONE
    │
    └─ Task 3: trigger_audit_pipeline
        └─ Triggers DE's audit DAG (kelmar_outbound) — independent, fire-and-forget
```

**Cloud Run:**
- URL: `https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app`
- Memory: 8Gi, CPU: 4, Timeout: 3600s
- Service account: `svc-omes-data-property-np@aw-ost-property-np.iam.gserviceaccount.com`

**Cloud Run deploy command:**
```bash
cd ~/Ost_entity_resolution/citizenmatch
gcloud builds submit --tag gcr.io/aw-ost-property-np/citizenmatch-pipeline .
gcloud run deploy citizenmatch-pipeline \
  --image gcr.io/aw-ost-property-np/citizenmatch-pipeline \
  --region us-central1 \
  --project aw-ost-property-np \
  --service-account svc-omes-data-property-np@aw-ost-property-np.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --timeout 3600 \
  --memory 8Gi \
  --cpu 4
```

**Composer:**
- Environment: `composer-ost-dev` (us-central1)
- DAG ID: `citizenmatch_pipeline`
- DAG file: `gs://us-central1-composer-ost-de-78128a51-bucket/dags/citizenmatch_pipeline.py`
- Schedule: `None` (manual) — change to `"0 7 1 * *"` for monthly production
- `execution_timeout`: 12 hours (covers full initial load)

**DAG upload command:**
```bash
cd ~/Ost_entity_resolution
gsutil cp composer/composer_cloud_run.py gs://us-central1-composer-ost-de-78128a51-bucket/dags/citizenmatch_pipeline.py
```

**GitHub:** `https://github.com/NomelEsso/Ost_entity_resolution.git` (private)

**Deploy workflow:**
1. Edit in VS Code → commit → push to GitHub
2. Cloud Shell: `cd ~/Ost_entity_resolution && git pull`
3. Build + deploy Cloud Run (commands above)
4. Upload DAG to Composer (command above)
5. Trigger: Airflow UI → citizenmatch_pipeline → ▶

---

## Known pitfalls (paid for in debug time — don't reintroduce)

1. **Silent null buckets from `.map()` rename.** Always assert `combined["bucket"].isna().sum() == 0` before writing to BigQuery.
2. **SOK fan-out on DLN joins.** Fix: `ROW_NUMBER() OVER (PARTITION BY DLN ORDER BY _data_file_date_ DESC NULLS LAST) = 1`.
3. **Capping must partition on `(OwnerID, PropertyID)`, not just `OwnerID`.** Cap is `= 1`.
4. **`DET_AUTO_APPROVE` can show lower confidence than `DET_REVIEW_*` rows.** Expected, not a bug.
5. **Block 2 will never auto-approve.** By design.
6. **`Transaction_ID` can be NULL.** Use `COALESCE(CAST(Transaction_ID AS STRING), '')` in keyset pagination.
7. **BirthDT vs Date_of_Birth type mismatch.** Kelmar `BirthDT` is STRING (`MM/DD/YYYY`), SOK `Date_of_Birth` is DATE. Join must use `SAFE.PARSE_DATE('%m/%d/%Y', k.BirthDT) = s.Date_of_Birth`.
8. **Pyarrow type inference.** Columns with mostly NULLs get inferred as INT64. `_write_to_bq` forces object columns to string (except score columns which stay numeric).
9. **BigQuery Hook location.** Composer's BigQueryHook must specify `location="us-central1"` or it cannot find tables. Without this, all `_bq_count` queries silently return 0.
10. **Cloud Run terminates containers with no active requests.** Fire-and-forget HTTP calls do NOT work. The Composer DAG must keep the connection alive (`timeout=3600`).
11. **HTTP response drops between Composer and Cloud Run.** ReadTimeout is caught and handled — DAG checks BigQuery for progress, then retries.
12. **GCS export as folder.** BigQuery `EXPORT DATA` creates sharded folders. Use Python `storage.Client` to upload a single clean CSV instead.
13. **SOK_MAX_ROWS = 1M is the safe per-trigger limit.** 6.5M rows in one trigger exceeds Cloud Run's 1-hour timeout. The DAG auto-loops.

---

## Clean slate command (delete all pipeline tables before test run)

```bash
bq rm -f aw-ost-property-np:citizen_match.sok_staging_dataset_tokenized_v2
bq rm -f aw-ost-property-np:citizen_match.sok_tokenization_checkpoint
bq rm -f aw-ost-property-np:citizen_match.kelmar_staging_dataset_tokenized_v1
bq rm -f aw-ost-property-np:citizen_match.kelmar_clean_v1
bq rm -f aw-ost-property-np:citizen_match.sok_clean_v1
bq rm -f aw-ost-property-np:citizen_match.sok_ssn_null_clean_v1
bq rm -f aw-ost-property-np:citizen_match.ssn_deterministic_matches_v1
bq rm -f aw-ost-property-np:citizen_match.kelmar_unmatched_v1
bq rm -f aw-ost-property-np:citizen_match.kelmar_fuzzy_v1
bq rm -f aw-ost-property-np:citizen_match.sok_fuzzy_pool_v1
bq rm -f aw-ost-property-np:citizen_match.fuzzy_block1_candidates_v1
bq rm -f aw-ost-property-np:citizen_match.fuzzy_block2_candidates_v1
bq rm -f aw-ost-property-np:citizen_match.fuzzy_block1_classified_v1
bq rm -f aw-ost-property-np:citizen_match.fuzzy_block2_classified_v1
bq rm -f aw-ost-property-np:citizen_match.treasury_match_review_v2
bq rm -f aw-ost-property-np:citizen_match.treasury_match_review_v3
bq rm -f aw-ost-property-np:citizen_match.treasury_match_review_capped_v1
bq rm -f aw-ost-property-np:citizen_match.treasury_match_review_capped_v2
bq rm -f aw-ost-property-np:citizen_match.treasury_unmatched_v1
bq rm -f aw-ost-property-np:citizen_mpi_result.OK_OST_OMES_OUTBOUND_DataMatch
```

---

## Validation queries

```bash
# Row counts
bq query --project_id=aw-ost-property-np --nouse_legacy_sql \
  "SELECT 'kelmar_clean' AS tbl, COUNT(*) AS cnt FROM \`aw-ost-property-np.citizen_match.kelmar_clean_v1\`
   UNION ALL SELECT 'sok_tokenized', COUNT(*) FROM \`aw-ost-property-np.citizen_match.sok_staging_dataset_tokenized_v2\`
   UNION ALL SELECT 'det_matches', COUNT(*) FROM \`aw-ost-property-np.citizen_match.ssn_deterministic_matches_v1\`
   UNION ALL SELECT 'review_table', COUNT(*) FROM \`aw-ost-property-np.citizen_match.treasury_match_review_v2\`
   UNION ALL SELECT 'unmatched', COUNT(*) FROM \`aw-ost-property-np.citizen_match.treasury_unmatched_v1\`
   UNION ALL SELECT 'mpi_output', COUNT(*) FROM \`aw-ost-property-np.citizen_mpi_result.OK_OST_OMES_OUTBOUND_DataMatch\`"

# Bucket distribution
bq query --project_id=aw-ost-property-np --nouse_legacy_sql \
  "SELECT bucket, COUNT(*) AS cnt FROM \`aw-ost-property-np.citizen_match.treasury_match_review_v2\` GROUP BY bucket ORDER BY cnt DESC"

# Join validation (should be 0 mismatches)
bq query --project_id=aw-ost-property-np --nouse_legacy_sql \
  "SELECT COUNTIF(k.OwnerID IS NULL) AS kelmar_not_found, COUNTIF(t.kelmar_name != k.full_name_clean) AS name_mismatch, COUNT(*) AS total
   FROM \`aw-ost-property-np.citizen_match.treasury_match_review_v2\` t
   LEFT JOIN \`aw-ost-property-np.citizen_match.kelmar_clean_v1\` k
     ON CAST(t.OwnerID AS INT64) = k.OwnerID AND CAST(t.PropertyID AS INT64) = k.PropertyID"
```

---

## Latest test results (5,000 Kelmar × 5.89M SOK)

- **4,570 matched (91.4%)**, 430 unmatched (8.6%)
- DET_AUTO_APPROVE: 539, DET_REVIEW_MINOR: 1,358, DET_REVIEW_MODERATE: 1,638
- DET_REVIEW_MISMATCH: 267, FUZZY_AUTO_APPROVE: 9, FUZZY_REVIEW: 150, FUZZY_REJECT: 24,211
- Join validation: 0 mismatches, 0 missing records
- BirthDT nulls (90%) and Transaction_ID nulls (96%) confirmed as source data, not join errors

---

## Mandatory guardrails for any code change

- **Cleaning parity:** BigQuery JS UDFs in `bq_udfs.py` must produce identical output to Python cleaners in `cleaners.py`. If you change one, change both.
- **All BigQuery writes go through `_write_to_bq` in `pipeline.py`.** Forces object columns to string (except scores), centralizes write logic.
- **Bucket and `match_flag` are assigned in exactly one place** — `assemble_review_table`.
- Any new SOK join on DLN must use `ROW_NUMBER() ... = 1` unless you explicitly want full history.
- Any DLP call must use `DLP_TEMPLATE_NAME` from config.
- Any threshold or weight must be imported from config.
- DLN must never appear in MPI output tables or GCS exports.
- New code must be project-portable: read `PROJECT_ID` from config, don't hardcode.
- Score columns (`name_score`, `street_score`, `confidence_score`, `composite_score`) are rounded to 1 decimal in MPI output only.

---

## Coding preferences

- One task at a time. Validate before moving on.
- Prefer complete, copy-paste-ready scripts over diffs.
- Ad hoc SQL → write it for the BigQuery console, not wrapped in Python.
- Keep written summaries tight and single-paragraph where possible.

---

## On the horizon

- Switch DAG schedule from `None` to `"0 7 1 * *"` for monthly production
- Move to production GCP project `aw-ost-property-p` when approved (create new DLP template, update `config.py`)
- Structured logging
- Archive tables with batch numbers
- Feedback channel back to Kelmar
