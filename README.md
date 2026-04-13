# OST Entity Resolution — CitizenMatch Pipeline

Matches **5,000 unclaimed property records from Kelmar** against Oklahoma citizen
records in the SOK dataset, producing review-ready match proposals for Treasury.

## Architecture

MoveIt → GCS → [Composer DAG 1] → BigQuery staging
↓
Cloud Run service
(clean → match → score)
↓
BigQuery output → [Composer DAG 2] → GCS → MoveIt → OST Storage

## Project Structure

├── citizenmatch/                Cloud Run service (Python + Flask)
│   ├── config.py                Table names, thresholds, GCP config
│   ├── cleaners.py              Name/address standardization functions
│   ├── pipeline.py              Core matching logic (6 stages)
│   ├── main.py                  Flask entry point (POST / → run pipeline)
│   ├── requirements.txt         Python dependencies
│   └── Dockerfile               Container image definition
├── composer/                    Airflow DAGs (deployed to Cloud Composer)
│   ├── composer_cloud_run.py    Trigger Cloud Run + poll audit table
│   └── composer_dataproc.py     Ephemeral Dataproc cluster for PySpark ETL
├── notebooks/                   Development reference
│   └── citizenmatch-cleaning-dev.ipynb
└── README.md

## Matching Strategy

### Layer 1 — Deterministic SSN Match
SSNs are tokenized via Cloud DLP (deterministic, same SSN → same token).
Raw SSNs are **never** stored in output tables or logs.
Produces the highest-confidence matches (~69.7% of all matched properties).

### Layer 2 — Fuzzy Block 1 (ZIP + Last Name + DOB)
Strict structured block for records without SSN matches.
Confidence ceiling: **95**. Can produce `FUZZY_AUTO_APPROVE`.

### Layer 3 — Fuzzy Block 2 (ZIP + Last Name)
Broader block without DOB requirement.
Confidence ceiling: **85**. Can **never** produce `FUZZY_AUTO_APPROVE` by design.

### Scoring
- **Composite score** = name similarity × 0.6 + street similarity × 0.4
- **Confidence score** = composite × (ceiling / 100)
- Uses `rapidfuzz.WRatio` for similarity measurement

### Bucket Classification
| Bucket | Meaning |
|---|---|
| `DET_AUTO_APPROVE` | SSN match, names match |
| `DET_REVIEW_MINOR` | SSN match, names similar (≥90) |
| `DET_REVIEW_MODERATE` | SSN match, names somewhat similar (≥80) |
| `DET_REVIEW_MISMATCH` | SSN match, names very different (<80) |
| `FUZZY_AUTO_APPROVE` | Fuzzy confidence ≥ 90 (Block 1 only) |
| `FUZZY_REVIEW` | Fuzzy confidence 80–89 |
| `FUZZY_REJECT` | Fuzzy confidence < 80 |

## Deployment

### Cloud Run Service

```bash
cd citizenmatch

gcloud functions deploy citizenmatch-pipeline \
  --gen2 \
  --runtime python310 \
  --source gs://<code-bucket> \
  --entry-point main \
  --trigger-http \
  --no-allow-unauthenticated \
  --region us-central1 \
  --timeout 3600s \
  --service-account <sa>@<project>.iam.gserviceaccount.com \
  --project <project-id> \
  --memory 1Gi
```

### Composer DAGs
Upload `composer/*.py` to your Composer environment's DAGs folder in GCS.

## Security

- Raw SSNs are **dropped from DataFrames immediately** after DLP tokenization
- Notebook `.ipynb` outputs must be cleared before committing
- Only `ssn_token` + `ssn_last4` are persisted — never the full SSN
- Service account impersonation is controlled via `IMPERSONATE_SA` env var

## Key Tables

| Table | Purpose |
|---|---|
| `kelmar_clean_v1` | Cleaned Kelmar records |
| `sok_clean_v1` | Cleaned SOK records (SSN-present) |
| `sok_ssn_null_clean_v1` | Cleaned SOK records (SSN-null) |
| `ssn_deterministic_matches_v1` | SSN token matches |
| `treasury_match_review_capped_v2` | Final delivery table |
| `treasury_unmatched_v1` | Kelmar records with no match |