"""
Central configuration for the CitizenMatch pipeline.
All table names, project IDs, and thresholds live here.
"""

import os

# ---------------------------------------------------------------------------
# GCP project & dataset
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "omes-solacc-citizenmatch-01-d")
LOCATION   = os.environ.get("LOCATION", "us-central1")
DATASET    = os.environ.get("DATASET", "citizen_match")

# Service account impersonation (empty string = use default credentials)
IMPERSONATE_SA = os.environ.get("IMPERSONATE_SA", "")

# ---------------------------------------------------------------------------
# DLP tokenization
# ---------------------------------------------------------------------------
DLP_TEMPLATE_NAME = (
    f"projects/{PROJECT_ID}/locations/{LOCATION}"
    "/deidentifyTemplates/3184577974128662341"
)
DLP_BATCH_SIZE = 200
MAX_RETRIES    = 6

# BigQuery chunked read size (SOK tokenization)
BQ_CHUNK_ROWS = 100_000

# ---------------------------------------------------------------------------
# Source tables
# ---------------------------------------------------------------------------
KELMAR_STAGING = f"{PROJECT_ID}.{DATASET}.kelmar_staging_dataset"
SOK_STAGING    = f"{PROJECT_ID}.{DATASET}.sok_staging_dataset"

# ---------------------------------------------------------------------------
# Intermediate tables (pipeline creates/overwrites these)
# ---------------------------------------------------------------------------
KELMAR_TOKENIZED  = f"{PROJECT_ID}.{DATASET}.kelmar_staging_dataset_tokenized_v1"
SOK_TOKENIZED     = f"{PROJECT_ID}.{DATASET}.sok_staging_dataset_tokenized_v2"
KELMAR_CLEAN      = f"{PROJECT_ID}.{DATASET}.kelmar_clean_v1"
SOK_CLEAN         = f"{PROJECT_ID}.{DATASET}.sok_clean_v1"
SOK_NULL_CLEAN    = f"{PROJECT_ID}.{DATASET}.sok_ssn_null_clean_v1"
KELMAR_UNMATCHED  = f"{PROJECT_ID}.{DATASET}.kelmar_unmatched_v1"
KELMAR_FUZZY      = f"{PROJECT_ID}.{DATASET}.kelmar_fuzzy_v1"
FUZZY_POOL        = f"{PROJECT_ID}.{DATASET}.sok_fuzzy_pool_v1"
BLOCK1_CANDIDATES = f"{PROJECT_ID}.{DATASET}.fuzzy_block1_candidates_v1"
BLOCK2_CANDIDATES = f"{PROJECT_ID}.{DATASET}.fuzzy_block2_candidates_v1"
BLOCK1_CLASSIFIED = f"{PROJECT_ID}.{DATASET}.fuzzy_block1_classified_v1"
BLOCK2_CLASSIFIED = f"{PROJECT_ID}.{DATASET}.fuzzy_block2_classified_v1"
DET_MATCHES       = f"{PROJECT_ID}.{DATASET}.ssn_deterministic_matches_v1"

# ---------------------------------------------------------------------------
# Output tables
# ---------------------------------------------------------------------------
REVIEW_TABLE      = f"{PROJECT_ID}.{DATASET}.treasury_match_review_v2"
REVIEW_ENRICHED   = f"{PROJECT_ID}.{DATASET}.treasury_match_review_v3"
REVIEW_CAPPED_V1  = f"{PROJECT_ID}.{DATASET}.treasury_match_review_capped_v1"
REVIEW_CAPPED_V2  = f"{PROJECT_ID}.{DATASET}.treasury_match_review_capped_v2"
DELIVERY_TABLE    = f"{PROJECT_ID}.{DATASET}.treasury_match_kelmar_delivery_v2"
UNMATCHED_TABLE   = f"{PROJECT_ID}.{DATASET}.treasury_unmatched_v1"

# ---------------------------------------------------------------------------
# Scoring thresholds
# ---------------------------------------------------------------------------
BLOCK1_CONFIDENCE_CAP = 95   # ZIP + Last + DOB
BLOCK2_CONFIDENCE_CAP = 85   # ZIP + Last only (never reaches AUTO_APPROVE)
AUTO_APPROVE_THRESHOLD = 90
REVIEW_THRESHOLD       = 80  # below this → FUZZY_REJECT

# Weights for composite score
NAME_WEIGHT   = 0.6
STREET_WEIGHT = 0.4