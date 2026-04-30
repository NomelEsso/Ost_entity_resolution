"""
Central configuration for the CitizenMatch pipeline.
All table names, project IDs, and thresholds live here.
"""

import os

PROJECT_ID = os.environ.get("PROJECT_ID", "aw-ost-property-np")
LOCATION   = os.environ.get("LOCATION", "us-central1")

STAGING_DATASET = os.environ.get("STAGING_DATASET", "citizen_staging")
OUTPUT_DATASET  = os.environ.get("OUTPUT_DATASET", "citizen_match")
MPI_DATASET     = os.environ.get("MPI_DATASET", "citizen_mpi_result")

IMPERSONATE_SA = os.environ.get("IMPERSONATE_SA", "")

DLP_TEMPLATE_NAME = (
    f"projects/{PROJECT_ID}/locations/{LOCATION}"
    "/deidentifyTemplates/4648784086040580254"
)
DLP_BATCH_SIZE = 200
MAX_RETRIES    = 6
BQ_CHUNK_ROWS  = 100_000

# SOK rows per trigger during initial load (Mode 1)
# 1M rows ≈ 45 minutes of DLP processing — fits within Cloud Run 1hr timeout
# Set to 0 via env var to remove limit (only if running outside Cloud Run)
# Mode 2 (incremental) ignores this — processes new dates regardless
SOK_MAX_ROWS = int(os.environ.get("SOK_MAX_ROWS", "2000000"))

SOK_STAGING    = f"{PROJECT_ID}.{STAGING_DATASET}.boost_staging"
KELMAR_STAGING = f"{PROJECT_ID}.{STAGING_DATASET}.OK_OST_OMES_DataMatch"

KELMAR_TOKENIZED  = f"{PROJECT_ID}.{OUTPUT_DATASET}.kelmar_staging_dataset_tokenized_v1"
SOK_TOKENIZED     = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_staging_dataset_tokenized_v2"
KELMAR_CLEAN      = f"{PROJECT_ID}.{OUTPUT_DATASET}.kelmar_clean_v1"
SOK_CLEAN         = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_clean_v1"
SOK_NULL_CLEAN    = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_ssn_null_clean_v1"
KELMAR_UNMATCHED  = f"{PROJECT_ID}.{OUTPUT_DATASET}.kelmar_unmatched_v1"
KELMAR_FUZZY      = f"{PROJECT_ID}.{OUTPUT_DATASET}.kelmar_fuzzy_v1"
FUZZY_POOL        = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_fuzzy_pool_v1"
BLOCK1_CANDIDATES = f"{PROJECT_ID}.{OUTPUT_DATASET}.fuzzy_block1_candidates_v1"
BLOCK2_CANDIDATES = f"{PROJECT_ID}.{OUTPUT_DATASET}.fuzzy_block2_candidates_v1"
BLOCK1_CLASSIFIED = f"{PROJECT_ID}.{OUTPUT_DATASET}.fuzzy_block1_classified_v1"
BLOCK2_CLASSIFIED = f"{PROJECT_ID}.{OUTPUT_DATASET}.fuzzy_block2_classified_v1"
DET_MATCHES       = f"{PROJECT_ID}.{OUTPUT_DATASET}.ssn_deterministic_matches_v1"

REVIEW_TABLE      = f"{PROJECT_ID}.{OUTPUT_DATASET}.treasury_match_review_v2"
REVIEW_ENRICHED   = f"{PROJECT_ID}.{OUTPUT_DATASET}.treasury_match_review_v3"
REVIEW_CAPPED_V1  = f"{PROJECT_ID}.{OUTPUT_DATASET}.treasury_match_review_capped_v1"
REVIEW_CAPPED_V2  = f"{PROJECT_ID}.{OUTPUT_DATASET}.treasury_match_review_capped_v2"
UNMATCHED_TABLE   = f"{PROJECT_ID}.{OUTPUT_DATASET}.treasury_unmatched_v1"

# Final MPI output — overwritten each run
MPI_OUTPUT_TABLE = f"{PROJECT_ID}.{MPI_DATASET}.OK_OST_OMES_OUTBOUND_DataMatch"

# GCS bucket for dated copies (never overwritten)
GCS_MPI_BUCKET   = "kelmar_outbound_files"
GCS_MPI_PATH     = "kelmar_mpi_files"

BLOCK1_CONFIDENCE_CAP  = 95
BLOCK2_CONFIDENCE_CAP  = 85
AUTO_APPROVE_THRESHOLD = 90
REVIEW_THRESHOLD       = 80
NAME_WEIGHT   = 0.6
STREET_WEIGHT = 0.4
