# OST Entity Resolution — Code Review

**Date:** 2026-04-27
**Reviewer:** Claude (Opus 4.7)
**Scope:** Full repo (`citizenmatch/`, `composer/`, `notebooks/`, `.github/`, README, `.gitignore`)
**Branch:** `main` @ `8d565d4`

---

## 1. Executive summary

The pipeline is well-structured and small enough to read end-to-end. Cleaning logic is isolated and unit-tested, the matching strategy is documented, and SSN handling follows the principle of dropping raw values immediately after tokenization. The main risks are operational rather than architectural:

1. **Production safety bomb** — `SOK_MAX_ROWS` defaults to `500` in `config.py`. If the env var is not set in the Cloud Run service, the pipeline silently truncates to 500 SOK rows and produces incorrect results without any error.
2. **Composer DAG is misleading** — its docstring says "Trigger Cloud Run + poll audit table" but the code is a synchronous blocking POST. A retry of a long-running Composer task will re-trigger an already-running pipeline.
3. **README and Dockerfile disagree** — README documents `gcloud functions deploy` (Cloud Functions Gen2) but the actual deployment is a Cloud Run container with a custom Dockerfile.
4. **No CI** — `claude.yml` is the only workflow. Tests aren't run automatically; `test_cleaners.py` uses `print` rather than pytest, so it can't fail a build.
5. **NULL semantics in `NOT IN`** — `identify_unmatched_kelmar` uses `WHERE ssn_token NOT IN (SELECT ...)`. If any row in the subquery has a NULL `ssn_token`, the predicate evaluates to UNKNOWN and the table comes out empty.

Severity legend used below: **P0** = data-correctness/security risk, fix before next prod run · **P1** = correctness or operability, fix this sprint · **P2** = quality/maintainability, fix when convenient.

---

## 2. Findings by severity

### P0 — fix before next production run

#### P0-1. `SOK_MAX_ROWS` default is a 500-row test cap (`citizenmatch/config.py:26`)
```python
SOK_MAX_ROWS = int(os.environ.get("SOK_MAX_ROWS", "500"))
```
Pipeline silently caps SOK at 500 rows whenever the env var is unset. The two consumers (`tokenize_sok`, `clean_sok_null_table`) both honor the cap. There is no log line warning that a cap is in effect at startup, only a per-stage "test limit reached" message that an operator may miss.

**Action:**
- Default to `0` (no limit) in `config.py`.
- Set `SOK_MAX_ROWS=500` only in a non-prod env or local run.
- Log the effective cap in `run_pipeline()` start banner: `"SOK_MAX_ROWS=%d (0 = unlimited)"`.
- Optional: add a sanity assertion that fails the pipeline if cap is non-zero in a prod-tagged environment (e.g., `if PROJECT_ID.endswith("-p") and SOK_MAX_ROWS: raise ...`).

#### P0-2. `NOT IN` against nullable subquery (`citizenmatch/pipeline.py:396`)
```sql
WHERE ssn_token NOT IN (
    SELECT DISTINCT ssn_token FROM `{DET_MATCHES}`
)
```
If any row in `DET_MATCHES` has `ssn_token IS NULL`, ANSI SQL returns UNKNOWN for every comparison and `kelmar_unmatched_v1` ends up empty — which then produces an empty fuzzy candidate set without any error. `KELMAR_CLEAN.ssn_token` itself can also be NULL (DLP can return empty for empty SSN), so it's two-sided risk.

**Action:** rewrite as a `LEFT JOIN ... WHERE m.ssn_token IS NULL` (the same pattern already used in `build_fuzzy_pool`), or `NOT EXISTS`. Same fix should be applied anywhere `NOT IN` references nullable columns.

#### P0-3. SQL injection in keyset pagination (`citizenmatch/pipeline.py:177-227`)
`tokenize_sok` interpolates `last_txn`, `last_dln`, `last_file`, `last_ssn` directly into the SQL string. Values come from BigQuery rows (so not attacker-controlled today), but an unescaped apostrophe in any cursor field will break the next chunk's query mid-run, killing tokenization with a confusing parse error. Tokens are deterministic strings from DLP and unlikely to contain quotes, but `Transaction_ID` and `_data_file_date_` come from upstream data.

**Action:** use BigQuery query parameters (`bigquery.ScalarQueryParameter`) and pass them via `job_config=QueryJobConfig(query_parameters=[...])`. Replace `f"AND ... > '{last_txn}'"` with `... > @last_txn`.

#### P0-4. Composer DAG retries can double-trigger pipeline (`composer/composer_cloud_run.py:24-55`)
```python
default_args = {"retries": 1, "retry_delay": timedelta(minutes=5)}
...
resp = requests.post(CLOUD_RUN_URL, ..., timeout=3600)
```
If the synchronous POST times out at 3600s but the pipeline is still running, Airflow will retry after 5 min and submit a second concurrent run. The pipeline is not concurrency-safe (every stage `CREATE OR REPLACE TABLE`s — two runs racing will corrupt intermediate state and likely the final delivery table).

**Action:** either
- Set `retries=0` and rely on manual re-trigger, **or**
- Implement the audit-table polling pattern that the docstring already promises (kick off the Cloud Run asynchronously, write a run-id row to an audit table, and use a Composer `BigQueryTableExistenceSensor` or polling sensor to wait for completion). This is also the only way to keep a Composer worker free during a 1-hour run.

Until that's done, also add a `max_active_runs=1` (already set — good) and a fail-fast guard inside `main.py` that rejects a new POST if a prior run is in progress (e.g., a sentinel row in BQ or a GCS lock file).

### P1 — fix this sprint

#### P1-1. README deployment instructions don't match the code (`README.md:64-81`)
README shows `gcloud functions deploy ... --gen2` but `citizenmatch/Dockerfile` and `main.py` are a Flask + gunicorn Cloud Run service. Anyone following the README will deploy something that doesn't match the test environment.

**Action:** replace the deploy block with the actual `gcloud run deploy --source .` (or `gcloud builds submit` + `gcloud run deploy --image`) command, including the env vars (`PROJECT_ID`, `SOK_MAX_ROWS`, `IMPERSONATE_SA`, dataset names) and the service account.

#### P1-2. README architecture diagram claims polling that doesn't exist
README line 8–13 shows "Composer DAG 1" → "Cloud Run service" → "BigQuery output" → "Composer DAG 2", and the `composer_cloud_run.py` docstring says "Trigger Cloud Run + poll audit table". Neither DAG actually polls — `composer_cloud_run.py` blocks on a synchronous request, and there is no DAG 2. Either the docs are aspirational (mark as TODO) or the code is missing.

**Action:** decide which is true. If polling is the design, see P0-4. If the synchronous wait is intentional, update the docstring and architecture diagram to say so.

#### P1-3. Pipeline is not idempotent on partial failure (`citizenmatch/pipeline.py:234-261`)
`tokenize_sok` chunks SOK with `WRITE_TRUNCATE` on first chunk, `WRITE_APPEND` on the rest. If the function crashes after chunk 5, a manual re-run will re-truncate from scratch (losing the work that succeeded) and the cursor state is lost (it lives in local Python). For a multi-thousand-row tokenization that costs DLP API calls, this is wasteful.

**Action:** persist the cursor in a small BigQuery state table (or a GCS object) and let the function resume. Alternatively, do all tokenization in BigQuery via the DLP `TokenizingFunctions` (server-side) so chunks are managed by BQ.

#### P1-4. `BQ_CHUNK_ROWS = 500` collides with `SOK_MAX_ROWS = 500` default
With both defaults, `tokenize_sok` writes exactly one chunk and stops. Any change to `BQ_CHUNK_ROWS` without thinking about `SOK_MAX_ROWS` (or vice versa) will surprise the next operator.

**Action:** after fixing P0-1, leave a comment near `BQ_CHUNK_ROWS` noting that the cap interacts with `SOK_MAX_ROWS`, or compute the page size as `min(BQ_CHUNK_ROWS, SOK_MAX_ROWS or BQ_CHUNK_ROWS)`.

#### P1-5. `datetime.utcnow()` is deprecated (`citizenmatch/pipeline.py:492, 787`)
Deprecated in Python 3.12 and slated for removal. Cloud Run's runtime is on 3.10 today (per Dockerfile), so it works, but Python upgrades will break the pipeline silently (returns naive timestamps that BQ may treat as local).

**Action:** `datetime.now(timezone.utc)` everywhere, and add `from datetime import timezone`.

#### P1-6. MPI output table name uses UTC date (`citizenmatch/pipeline.py:787-789`)
`OK_OST_OMES_Output_DataMatch_<YYYYMMDD>` uses `datetime.utcnow()`. Pipeline runs in the early-morning Central window (per `composer_dataproc.py:36`); a run at 23:30 CST lands as the next day in UTC, which is confusing for downstream Treasury consumers.

**Action:** use America/Chicago to format the date suffix (Composer convention is already Chicago).

#### P1-7. Mixed BQ-write APIs (`citizenmatch/pipeline.py`)
Some writes use `_write_to_bq` (which forces object→string), others use `df.to_gbq(..., if_exists="replace")` (which doesn't). Result: schema drift between intermediate tables. The recent fix `8d565d4 fix: preserve numeric types for score columns` was needed precisely because both paths exist.

**Action:** consolidate on `_write_to_bq`. Have it accept an explicit BQ schema for the score-bearing tables instead of guessing from dtypes. Then `block1_classified` / `block2_classified` / `treasury_match_review_v2` all flow through the same path with deterministic types.

#### P1-8. Stray indentation in `_write_to_bq` (`citizenmatch/pipeline.py:86`)
The comment `# Force ambiguous object columns to string, but preserve numeric columns` is at column 0 inside the function. It still parses (Python comments can be at any column), but every reader will pause to check.

**Action:** indent it to match the function body.

#### P1-9. Unit-test runner is print-based (`citizenmatch/test_cleaners.py`)
The tests call `print` and a sentinel `if all(results)` at the bottom — they do not exit non-zero on failure (they simply print "SOME TESTS FAILED"). A CI invocation of `python test_cleaners.py` will exit 0 regardless.

**Action:** convert to `pytest` style (`assert result == expected`), name file `test_cleaners.py` so pytest discovers it, and wire a workflow that runs `pytest citizenmatch/`.

#### P1-10. Container runs as root (`citizenmatch/Dockerfile`)
No `USER` directive. Cloud Run mitigates the blast radius (read-only FS, no privileged caps), but the project's security posture (handling SSNs) warrants a non-root user.

**Action:**
```dockerfile
RUN useradd -m -u 1000 app
USER app
```
Place after `COPY` lines and before the `CMD`.

#### P1-11. Pipeline imports inside request handler (`citizenmatch/main.py:34`)
```python
from pipeline import run_pipeline
```
Lazy import means the first POST pays the import cost (numpy/pandas/google-cloud) and any import-time error returns a generic 500 instead of failing container startup. Cloud Run's "fail fast at boot" guarantees are weakened.

**Action:** move the import to the module top.

#### P1-12. Notebook is committed but not stripped automatically
`citizenmatch-cleaning-dev.ipynb` happens to have empty `outputs` today, but `.gitignore` only excludes `notebooks/.ipynb_checkpoints/`. If anyone commits a notebook with PII in cell output, it ships.

**Action:** add a pre-commit hook (`nbstripout --install`) and document it in README. Or move the notebook out of git and into a separate workspace.

### P2 — quality / maintainability

#### P2-1. Outdated dependencies (`citizenmatch/requirements.txt`)
All pins are from Q3 2024. Some are notably old:
- `pandas==2.2.2` (current 2.2.x is fine)
- `numpy==2.1.0` (numpy 2.x has known compatibility quirks with older google libs)
- `google-cloud-bigquery==3.25.0`
- `google-auth==2.36.0`

`composer/composer_dataproc.py:49` pins `pyarrow==10.0.1` while the Cloud Run service pins `pyarrow==17.0.0` — a 7-major-version gap on the same wire format is a footgun if intermediate Avro/Parquet ever crosses both.

**Action:** add a Dependabot config (`.github/dependabot.yml`) for `pip` ecosystem. Refresh once and pin again.

#### P2-2. Hardcoded DLP template ID (`citizenmatch/config.py:17-20`)
`deidentifyTemplates/4648784086040580254` is project-specific and not env-driven. Promoting to a new project requires editing code.

**Action:** make it env-driven (`DLP_TEMPLATE_ID` env var, default to current value).

#### P2-3. Row-by-row rapidfuzz scoring (`citizenmatch/pipeline.py:472-479`)
```python
df["name_score"] = df.apply(lambda r: similarity(...), axis=1)
```
For 5k Kelmar × N SOK candidates per ZIP, this is fine. But if the SOK side grows, `rapidfuzz.process.cdist` is 10–100× faster.

**Action:** profile first; only switch if either block ever produces > 50k candidate pairs.

#### P2-4. Generic exception handling in DLP retry (`citizenmatch/pipeline.py:138`)
```python
except Exception as e:
    if "ResourceExhausted" in str(e) or "429" in str(e):
```
String-matching on exception messages is brittle. The library raises specific exception classes (`google.api_core.exceptions.ResourceExhausted`, `TooManyRequests`).

**Action:**
```python
from google.api_core import exceptions as gapi
except gapi.ResourceExhausted:
    ...
except gapi.TooManyRequests:
    ...
```

#### P2-5. `assemble_review_table` loads everything into memory (`citizenmatch/pipeline.py:612-613`)
Block 1 + Block 2 candidates plus deterministic matches all materialize as DataFrames, get concatenated, deduped, and written back. With current 5k Kelmar this is fine; if Kelmar ever scales, this is the next bottleneck.

**Action:** keep an eye on it; fold the dedup into BigQuery SQL if memory becomes an issue.

#### P2-6. No structured logging context
Every stage prints its own log lines but there's no run-id correlating them. When two runs overlap (which P0-4 makes possible), the logs are unreadable.

**Action:** generate a `run_id = uuid4().hex[:8]` at the top of `run_pipeline`, add it to a `logging.Filter` so every line carries `[run_id=xxxx]`. Stamp it on the MPI output table name too.

#### P2-7. README "Key Tables" missing tokenized tables
README lists the cleaned and final tables but skips `kelmar_staging_dataset_tokenized_v1`, `sok_staging_dataset_tokenized_v2`, the fuzzy candidates/classified tables, and the dated MPI output. New engineers won't know what they're looking at in BQ.

**Action:** add the missing tables, or link to `config.py` as the source of truth.

#### P2-8. `composer_dataproc.py` placeholders all over
Every project/service-account/bucket is `<placeholder>`. Either the file is a template (mark it as `.template.py`) or it should reference Airflow Variables / Secret Manager.

**Action:** convert to `Variable.get("dataproc_project_id")` etc., or rename to make it obvious this is a template.

#### P2-9. No `__init__.py` in `citizenmatch/`
Tests import `from cleaners import ...` (relative, works only when CWD is `citizenmatch/`). Once you add pytest in CI, pick a shape: either keep flat scripts (and run pytest from `citizenmatch/`), or make it a package (`citizenmatch/__init__.py`, imports become `from citizenmatch.cleaners import ...`).

**Action:** decide and document in README "Running tests".

#### P2-10. Health check returns 200 unconditionally (`citizenmatch/main.py:46-48`)
GET `/` returns `{"status": "healthy"}` even if BQ creds are bad. Cloud Run's startup probe will pass, requests will fail.

**Action:** at minimum, exercise `_get_bq_client()` once at boot and fail container startup if it raises. Optional: add a `/ready` that does a `bq.query("SELECT 1").result()` on demand.

#### P2-11. `clean_name` strips titles but not generational suffixes (cleaners.py:35)
`TITLE_RE` removes prefix titles only. JR/SR/II/III suffixes pass through, which is mostly correct (they help disambiguate matches), but document the choice — at least one test (`test_clean_name`) silently depends on `JR` not being stripped.

**Action:** add a one-line comment in `cleaners.py` near `TITLE_RE` stating that suffixes are intentionally preserved.

#### P2-12. CI workflow missing for tests/lint
`.github/workflows/claude.yml` only runs the Claude action. There's no workflow that runs `pytest`, `ruff`, or `mypy` on a PR.

**Action:** add `.github/workflows/ci.yml` that runs `pytest citizenmatch/` and `ruff check .` on push/PR. This is a prerequisite for P1-9 to actually catch regressions.

---

## 3. Cross-cutting observations

- **Security posture is solid.** SSN handling — DLP tokenization, dropping the raw column on the same line, persisting only `ssn_token` + `ssn_last4` — is the strongest part of the codebase. Don't regress it.
- **Type discipline is the recurring pain point.** Most of the recent commit history is about coercing types between BQ ↔ pandas (`d472f6b`, `8d565d4`). Centralizing this in `_write_to_bq` with explicit per-table schemas (P1-7) will save future commits.
- **Operational story is the weakest part.** No CI, no idempotency on partial failure, retries that double-trigger, ambient defaults that quietly truncate data, and a Composer DAG that disagrees with its own docstring. P0-1 + P0-4 + P1-1 are the trio that bite you on day one of production.

---

## 4. Recommended next steps (ordered)

1. Fix `SOK_MAX_ROWS` default → `0` and log the effective cap (P0-1). One-line change, biggest blast-radius reduction.
2. Rewrite `identify_unmatched_kelmar` to `LEFT JOIN` (P0-2). Five-line SQL change.
3. Set `composer_cloud_run.py` retries to 0 *today*; design the polling pattern as a follow-up (P0-4).
4. Update README deploy instructions to Cloud Run (P1-1) and reconcile the architecture diagram (P1-2).
5. Add a `pytest` + `ruff` CI workflow (P2-12) and convert `test_cleaners.py` (P1-9). After that, every other fix lands with regression coverage.
6. Parameterize the keyset-pagination query (P0-3) and replace `datetime.utcnow()` (P1-5).
7. Backlog the rest (consolidate BQ writers, run-id logging, dependency refresh, non-root container).
