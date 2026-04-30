"""
CitizenMatch pipeline — core matching logic.

Stages:
  1. Tokenize SSNs (Kelmar + SOK) via Cloud DLP
  2. Clean both datasets
  3. Deterministic SSN matching
  4. Fuzzy matching (Block 1: ZIP+Last+DOB, Block 2: ZIP+Last)
  5. Combine, dedupe, enrich, cap candidates
  6. Produce delivery + unmatched tables
  7. Publish MPI output (BQ overwrite + GCS dated copy)

Called by main.py via run_pipeline().
"""

import logging
import time
from datetime import datetime

import numpy as np
import pandas as pd
from google.cloud import bigquery, dlp_v2, storage
import google.auth
from google.auth import impersonated_credentials as ic

from config import (
    PROJECT_ID, LOCATION, OUTPUT_DATASET, IMPERSONATE_SA,
    DLP_TEMPLATE_NAME, DLP_BATCH_SIZE, MAX_RETRIES, BQ_CHUNK_ROWS,
    KELMAR_STAGING, SOK_STAGING, SOK_MAX_ROWS,
    KELMAR_TOKENIZED, SOK_TOKENIZED,
    KELMAR_CLEAN, SOK_CLEAN, SOK_NULL_CLEAN,
    KELMAR_UNMATCHED, KELMAR_FUZZY, FUZZY_POOL,
    BLOCK1_CANDIDATES, BLOCK2_CANDIDATES,
    BLOCK1_CLASSIFIED, BLOCK2_CLASSIFIED,
    DET_MATCHES,
    REVIEW_TABLE, REVIEW_ENRICHED,
    REVIEW_CAPPED_V1, REVIEW_CAPPED_V2,
    UNMATCHED_TABLE,
    MPI_OUTPUT_TABLE, GCS_MPI_BUCKET, GCS_MPI_PATH,
    BLOCK1_CONFIDENCE_CAP, BLOCK2_CONFIDENCE_CAP,
    AUTO_APPROVE_THRESHOLD, REVIEW_THRESHOLD,
    NAME_WEIGHT, STREET_WEIGHT,
)
from cleaners import similarity
from bq_udfs import create_cleaning_udfs

log = logging.getLogger(__name__)


# ===========================================================================
# BigQuery / DLP client setup
# ===========================================================================

def _get_bq_client():
    """Return a BigQuery client, optionally using SA impersonation."""
    if IMPERSONATE_SA:
        source_creds, _ = google.auth.default()
        target_creds = ic.Credentials(
            source_credentials=source_creds,
            target_principal=IMPERSONATE_SA,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return bigquery.Client(project=PROJECT_ID, credentials=target_creds)
    return bigquery.Client(project=PROJECT_ID)


def _get_dlp_client():
    """Return a DLP client, optionally using SA impersonation."""
    if IMPERSONATE_SA:
        source_creds, _ = google.auth.default()
        target_creds = ic.Credentials(
            source_credentials=source_creds,
            target_principal=IMPERSONATE_SA,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return dlp_v2.DlpServiceClient(credentials=target_creds)
    return dlp_v2.DlpServiceClient()


def _write_to_bq(bq, df, table_id, truncate=True):
    """Load a DataFrame into BigQuery."""
    disposition = "WRITE_TRUNCATE" if truncate else "WRITE_APPEND"
    # Force ambiguous object columns to string, but preserve numeric columns
    for col in df.select_dtypes(include=["object"]).columns:
        if col not in ("name_score", "street_score", "confidence_score", "composite_score"):
            df[col] = df[col].astype("string")
    job = bq.load_table_from_dataframe(
        df, table_id,
        job_config=bigquery.LoadJobConfig(write_disposition=disposition),
    )
    job.result()


# ===========================================================================
# Stage 1: DLP tokenization
# ===========================================================================

def _tokenize_batch(dlp, ssns, batch_size=DLP_BATCH_SIZE):
    """Tokenise a list of SSN strings via Cloud DLP TABLE item.

    Returns a list of token strings (same length as input).
    """
    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
    out = [None] * len(ssns)

    for start in range(0, len(ssns), batch_size):
        end = min(start + batch_size, len(ssns))
        batch = ssns[start:end]

        table_item = {
            "table": {
                "headers": [{"name": "SSN"}],
                "rows": [
                    {"values": [{"string_value": ("" if pd.isna(x) else str(x).strip())}]}
                    for x in batch
                ],
            }
        }

        delay = 1.0
        for attempt in range(MAX_RETRIES):
            try:
                resp = dlp.deidentify_content(
                    request={
                        "parent": parent,
                        "deidentify_template_name": DLP_TEMPLATE_NAME,
                        "item": table_item,
                    }
                )
                out[start:end] = [
                    r.values[0].string_value for r in resp.item.table.rows
                ]
                break
            except Exception as e:
                if "ResourceExhausted" in str(e) or "429" in str(e):
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        else:
            raise RuntimeError(f"DLP failed after {MAX_RETRIES} retries ({start}:{end})")

    return out


def tokenize_kelmar(bq, dlp):
    """Tokenize all Kelmar SSNs → kelmar_staging_dataset_tokenized_v1."""
    log.info("Tokenizing Kelmar SSNs...")

    df = bq.query(f"""
        SELECT OwnerID, PropertyID,
               NameLast, NameFirst, NameMiddle,
               Address1, CAST(Address2 AS STRING) AS Address2, CAST(Address3 AS STRING) AS Address3,
               City, State, Zip,
               CAST(SSN AS STRING) AS SSN,
               BirthDT, CashValue
        FROM `{KELMAR_STAGING}`
    """).to_dataframe(create_bqstorage_client=True)

    df["SSN"] = df["SSN"].astype("string").str.strip()
    df["ssn_token"] = _tokenize_batch(dlp, df["SSN"].tolist())
    df["ssn_last4"] = df["SSN"].str.replace(r"\D", "", regex=True).str[-4:]
    df = df.drop(columns=["SSN"])  # ⛔ never persist raw SSN

    _write_to_bq(bq, df, KELMAR_TOKENIZED)
    log.info("✅ Kelmar tokenized: %d rows → %s", len(df), KELMAR_TOKENIZED)


def tokenize_sok(bq, dlp):
    """Tokenize SOK SSNs with checkpoint-based resume → sok_staging_dataset_tokenized_v2.

    Two modes:
      1. INITIAL LOAD: checkpoint exists or no data yet → keyset pagination,
         saves cursor after each chunk, resumes on next trigger.
         Deletes checkpoint when all rows are processed.
      2. INCREMENTAL (monthly): no checkpoint + data exists → only processes
         _data_file_date_ values not yet in the tokenized table.
    """
    log.info("Tokenizing SOK SSNs...")

    CHECKPOINT_TABLE = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_tokenization_checkpoint"

    # --- Determine mode ---
    has_checkpoint = False
    last_txn = None
    last_dln = last_file = last_ssn = ""
    existing_count = 0

    try:
        existing_count = int(bq.query(
            f"SELECT COUNT(*) AS n FROM `{SOK_TOKENIZED}`"
        ).to_dataframe()["n"].iloc[0])
    except Exception:
        pass

    try:
        cp = bq.query(f"SELECT * FROM `{CHECKPOINT_TABLE}` LIMIT 1").to_dataframe()
        if not cp.empty:
            has_checkpoint = True
            last_txn  = cp["last_txn"].iloc[0]
            last_dln  = cp["last_dln"].iloc[0]
            last_file = cp["last_file"].iloc[0]
            last_ssn  = cp["last_ssn"].iloc[0]
    except Exception:
        pass

    # =====================================================================
    # MODE 1: INITIAL LOAD (checkpoint exists OR no data yet)
    # =====================================================================
    if has_checkpoint or existing_count == 0:
        if has_checkpoint:
            log.info("  INITIAL LOAD — resuming from checkpoint (%d rows exist)", existing_count)
            first_write = False
        else:
            log.info("  INITIAL LOAD — starting fresh")
            first_write = True

        def _build_cursor_filter():
            if last_txn is None:
                return ""
            return f"""
            AND (
              COALESCE(CAST(Transaction_ID AS STRING), '') > '{last_txn}'
              OR (
                COALESCE(CAST(Transaction_ID AS STRING), '') = '{last_txn}'
                AND (
                  CAST(DLN AS STRING) > '{last_dln}'
                  OR (
                    CAST(DLN AS STRING) = '{last_dln}'
                    AND (
                      COALESCE(CAST(_data_file_date_ AS STRING), '') > '{last_file}'
                      OR (
                        COALESCE(CAST(_data_file_date_ AS STRING), '') = '{last_file}'
                        AND CAST(SSN AS STRING) > '{last_ssn}'
                      )
                    )
                  )
                )
              )
            )
            """

        pending = bq.query(f"""
            SELECT COUNT(*) AS n FROM `{SOK_STAGING}`
            WHERE SSN IS NOT NULL AND DLN IS NOT NULL
            {_build_cursor_filter()}
        """).to_dataframe()["n"].iloc[0]

        if pending == 0:
            log.info("  initial load complete — deleting checkpoint")
            try:
                bq.query(f"DROP TABLE IF EXISTS `{CHECKPOINT_TABLE}`").result()
            except Exception:
                pass
            log.info("✅ SOK fully tokenized: %d rows → %s", existing_count, SOK_TOKENIZED)
            return

        log.info("  %d rows remaining", pending)

        def _fetch_chunk():
            return bq.query(f"""
                SELECT
                  COALESCE(CAST(Transaction_ID AS STRING), '') AS Transaction_ID,
                  COALESCE(CAST(_data_file_date_ AS STRING), '') AS _data_file_date_,
                  Transaction_Date, Transaction_Type,
                  CAST(DLN AS STRING) AS DLN,
                  CAST(SSN AS STRING) AS SSN,
                  First_Name, Middle_Name, Last_Name, Suffix, Date_of_Birth,
                  Residential_Address_Street,
                  CAST(Residential_Address_Street_2 AS STRING) AS Residential_Address_Street_2,
                  CAST(Residential_Address_Unit_Type AS STRING) AS Residential_Address_Unit_Type,
                  CAST(Residential_Address_Unit AS STRING) AS Residential_Address_Unit,
                  Residential_Address_City, Residential_Address_State, Residential_Address_Zip,
                  Mailing_Address_Street,
                  CAST(Mailing_Address_Street_2 AS STRING) AS Mailing_Address_Street_2,
                  CAST(Mailing_Address_Unit_Type AS STRING) AS Mailing_Address_Unit_Type,
                  CAST(Mailing_Address_Unit AS STRING) AS Mailing_Address_Unit,
                  Mailing_Address_City, Mailing_Address_State, Mailing_Address_Zip
                FROM `{SOK_STAGING}`
                WHERE SSN IS NOT NULL AND DLN IS NOT NULL
                {_build_cursor_filter()}
                ORDER BY
                  COALESCE(CAST(Transaction_ID AS STRING), ''),
                  CAST(DLN AS STRING),
                  COALESCE(CAST(_data_file_date_ AS STRING), ''),
                  CAST(SSN AS STRING)
                LIMIT {BQ_CHUNK_ROWS}
            """).to_dataframe(create_bqstorage_client=True)

        def _save_checkpoint():
            cp_df = pd.DataFrame([{
                "last_txn": last_txn,
                "last_dln": last_dln,
                "last_file": last_file,
                "last_ssn": last_ssn,
                "updated_at": datetime.utcnow().isoformat(),
            }])
            _write_to_bq(bq, cp_df, CHECKPOINT_TABLE, truncate=True)

        total = 0
        while True:
            chunk = _fetch_chunk()
            if chunk.empty:
                # All done — delete checkpoint
                log.info("  initial load complete — deleting checkpoint")
                try:
                    bq.query(f"DROP TABLE IF EXISTS `{CHECKPOINT_TABLE}`").result()
                except Exception:
                    pass
                break

            chunk["SSN"] = chunk["SSN"].astype("string").str.strip()
            chunk["ssn_token"] = _tokenize_batch(dlp, chunk["SSN"].tolist())
            chunk["ssn_last4"] = chunk["SSN"].str.replace(r"\D", "", regex=True).str[-4:]

            last_txn  = chunk["Transaction_ID"].iloc[-1]
            last_dln  = chunk["DLN"].iloc[-1]
            last_file = chunk["_data_file_date_"].iloc[-1]
            last_ssn  = chunk["SSN"].iloc[-1]

            chunk = chunk.drop(columns=["SSN"])

            _write_to_bq(bq, chunk, SOK_TOKENIZED, truncate=first_write)
            first_write = False
            total += len(chunk)

            _save_checkpoint()
            log.info("  wrote %d rows (total new: %d) — checkpoint saved", len(chunk), total)

            if SOK_MAX_ROWS and total >= SOK_MAX_ROWS:
                log.info("  per-run limit reached (%d rows) — resume on next trigger", SOK_MAX_ROWS)
                break

        log.info("✅ SOK tokenized: %d new rows (total: %d) → %s",
                 total, existing_count + total, SOK_TOKENIZED)
        return

    # =====================================================================
    # MODE 2: INCREMENTAL (no checkpoint + data exists = initial load done)
    # =====================================================================
    log.info("  INCREMENTAL — checking for new _data_file_date_ values")

    new_dates = bq.query(f"""
        SELECT DISTINCT CAST(_data_file_date_ AS STRING) AS file_date
        FROM `{SOK_STAGING}`
        WHERE SSN IS NOT NULL AND DLN IS NOT NULL
          AND CAST(_data_file_date_ AS STRING) NOT IN (
            SELECT DISTINCT _data_file_date_ FROM `{SOK_TOKENIZED}`
          )
        ORDER BY file_date
    """).to_dataframe()["file_date"].tolist()

    if not new_dates:
        log.info("  no new dates to process — SOK is up to date")
        log.info("✅ SOK tokenized: %d rows (up to date) → %s", existing_count, SOK_TOKENIZED)
        return

    log.info("  %d new date(s) to process: %s", len(new_dates), new_dates)

    total = 0
    for file_date in new_dates:
        log.info("  processing date: %s", file_date)

        date_df = bq.query(f"""
            SELECT
              COALESCE(CAST(Transaction_ID AS STRING), '') AS Transaction_ID,
              COALESCE(CAST(_data_file_date_ AS STRING), '') AS _data_file_date_,
              Transaction_Date, Transaction_Type,
              CAST(DLN AS STRING) AS DLN,
              CAST(SSN AS STRING) AS SSN,
              First_Name, Middle_Name, Last_Name, Suffix, Date_of_Birth,
              Residential_Address_Street,
              CAST(Residential_Address_Street_2 AS STRING) AS Residential_Address_Street_2,
              CAST(Residential_Address_Unit_Type AS STRING) AS Residential_Address_Unit_Type,
              CAST(Residential_Address_Unit AS STRING) AS Residential_Address_Unit,
              Residential_Address_City, Residential_Address_State, Residential_Address_Zip,
              Mailing_Address_Street,
              CAST(Mailing_Address_Street_2 AS STRING) AS Mailing_Address_Street_2,
              CAST(Mailing_Address_Unit_Type AS STRING) AS Mailing_Address_Unit_Type,
              CAST(Mailing_Address_Unit AS STRING) AS Mailing_Address_Unit,
              Mailing_Address_City, Mailing_Address_State, Mailing_Address_Zip
            FROM `{SOK_STAGING}`
            WHERE SSN IS NOT NULL AND DLN IS NOT NULL
              AND CAST(_data_file_date_ AS STRING) = '{file_date}'
        """).to_dataframe(create_bqstorage_client=True)

        if date_df.empty:
            continue

        date_df["SSN"] = date_df["SSN"].astype("string").str.strip()
        date_df["ssn_token"] = _tokenize_batch(dlp, date_df["SSN"].tolist())
        date_df["ssn_last4"] = date_df["SSN"].str.replace(r"\D", "", regex=True).str[-4:]
        date_df = date_df.drop(columns=["SSN"])

        _write_to_bq(bq, date_df, SOK_TOKENIZED, truncate=False)
        total += len(date_df)
        log.info("  date %s: %d rows tokenized", file_date, len(date_df))

    log.info("✅ SOK incremental: %d new rows (total: %d) → %s",
             total, existing_count + total, SOK_TOKENIZED)


# ===========================================================================
# Stage 2: Cleaning
# ===========================================================================

def clean_sok_table(bq):
    """Clean SOK tokenized (SSN-present) rows via BigQuery SQL — zero memory footprint."""
    log.info("Cleaning SOK (SSN-present) via SQL...")

    udf = f"{PROJECT_ID}.{OUTPUT_DATASET}"
    bq.query(f"""
        CREATE OR REPLACE TABLE `{SOK_CLEAN}` AS
        SELECT *,
            `{udf}`.clean_name(First_Name)  AS first_name_clean,
            `{udf}`.clean_name(Middle_Name) AS middle_name_clean,
            `{udf}`.clean_name(Last_Name)   AS last_name_clean,
            NULLIF(TRIM(REGEXP_REPLACE(
                CONCAT(
                    COALESCE(`{udf}`.clean_name(First_Name), ''), ' ',
                    COALESCE(`{udf}`.clean_name(Middle_Name), ''), ' ',
                    COALESCE(`{udf}`.clean_name(Last_Name), '')
                ), r'\\s+', ' '
            )), '') AS full_name_clean,
            `{udf}`.clean_street(
                Residential_Address_Street,
                CAST(Residential_Address_Street_2 AS STRING),
                NULL
            ) AS street_clean,
            `{udf}`.clean_name(Residential_Address_City)    AS city_clean,
            `{udf}`.clean_state(Residential_Address_State)  AS state_clean,
            `{udf}`.clean_zip(CAST(Residential_Address_Zip AS STRING)) AS zip_clean
        FROM `{SOK_TOKENIZED}`
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{SOK_CLEAN}`").to_dataframe()
    log.info("✅ SOK cleaned: %s rows → %s", count["n"].iloc[0], SOK_CLEAN)


def clean_kelmar_table(bq):
    """Clean Kelmar tokenized rows via BigQuery SQL — zero memory footprint."""
    log.info("Cleaning Kelmar via SQL...")

    udf = f"{PROJECT_ID}.{OUTPUT_DATASET}"
    bq.query(f"""
        CREATE OR REPLACE TABLE `{KELMAR_CLEAN}` AS
        SELECT *,
            `{udf}`.clean_name(NameFirst)  AS first_name_clean,
            `{udf}`.clean_name(NameMiddle) AS middle_name_clean,
            `{udf}`.clean_name(NameLast)   AS last_name_clean,
            NULLIF(TRIM(REGEXP_REPLACE(
                CONCAT(
                    COALESCE(`{udf}`.clean_name(NameFirst), ''), ' ',
                    COALESCE(`{udf}`.clean_name(NameMiddle), ''), ' ',
                    COALESCE(`{udf}`.clean_name(NameLast), '')
                ), r'\\s+', ' '
            )), '') AS full_name_clean,
            `{udf}`.clean_street(
                Address1,
                CAST(Address2 AS STRING),
                CAST(Address3 AS STRING)
            ) AS street_clean,
            `{udf}`.clean_name(City)           AS city_clean,
            `{udf}`.clean_state(State)         AS state_clean,
            `{udf}`.clean_zip(CAST(Zip AS STRING)) AS zip_clean
        FROM `{KELMAR_TOKENIZED}`
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{KELMAR_CLEAN}`").to_dataframe()
    log.info("✅ Kelmar cleaned: %s rows → %s", count["n"].iloc[0], KELMAR_CLEAN)


def clean_sok_null_table(bq):
    """Extract and clean SOK SSN-null rows via BigQuery SQL — zero memory footprint."""
    log.info("Cleaning SOK (SSN-null) via SQL...")

    udf = f"{PROJECT_ID}.{OUTPUT_DATASET}"
    limit_clause = f"LIMIT {SOK_MAX_ROWS}" if SOK_MAX_ROWS else ""
    bq.query(f"""
        CREATE OR REPLACE TABLE `{SOK_NULL_CLEAN}` AS
        SELECT
          COALESCE(CAST(Transaction_ID AS STRING), '') AS Transaction_ID,
          COALESCE(CAST(_data_file_date_ AS STRING), '') AS _data_file_date_,
          Transaction_Date, Transaction_Type,
          CAST(DLN AS STRING) AS DLN,
          First_Name, Middle_Name, Last_Name, Suffix,
          Date_of_Birth,
          Residential_Address_Street,
          CAST(Residential_Address_Street_2 AS STRING) AS Residential_Address_Street_2,
          CAST(Residential_Address_Unit_Type AS STRING) AS Residential_Address_Unit_Type,
          CAST(Residential_Address_Unit AS STRING) AS Residential_Address_Unit,
          Residential_Address_City, Residential_Address_State, Residential_Address_Zip,
          Mailing_Address_Street,
          CAST(Mailing_Address_Street_2 AS STRING) AS Mailing_Address_Street_2,
          CAST(Mailing_Address_Unit_Type AS STRING) AS Mailing_Address_Unit_Type,
          CAST(Mailing_Address_Unit AS STRING) AS Mailing_Address_Unit,
          Mailing_Address_City, Mailing_Address_State, Mailing_Address_Zip,
          `{udf}`.clean_name(First_Name)  AS first_name_clean,
          `{udf}`.clean_name(Middle_Name) AS middle_name_clean,
          `{udf}`.clean_name(Last_Name)   AS last_name_clean,
          NULLIF(TRIM(REGEXP_REPLACE(
              CONCAT(
                  COALESCE(`{udf}`.clean_name(First_Name), ''), ' ',
                  COALESCE(`{udf}`.clean_name(Middle_Name), ''), ' ',
                  COALESCE(`{udf}`.clean_name(Last_Name), '')
              ), r'\\s+', ' '
          )), '') AS full_name_clean,
          `{udf}`.clean_street(
              Residential_Address_Street,
              CAST(Residential_Address_Street_2 AS STRING),
              NULL
          ) AS street_clean,
          `{udf}`.clean_name(Residential_Address_City)    AS city_clean,
          `{udf}`.clean_state(Residential_Address_State)  AS state_clean,
          `{udf}`.clean_zip(CAST(Residential_Address_Zip AS STRING)) AS zip_clean
        FROM `{SOK_STAGING}`
        WHERE SSN IS NULL
          AND DLN IS NOT NULL
        {limit_clause}
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{SOK_NULL_CLEAN}`").to_dataframe()
    log.info("✅ SOK SSN-null cleaned: %s rows → %s", count["n"].iloc[0], SOK_NULL_CLEAN)


# ===========================================================================
# Stage 3: Deterministic SSN matching
# ===========================================================================

def deterministic_match(bq):
    """Join Kelmar ↔ SOK on ssn_token (deduped by DLN) → ssn_deterministic_matches_v1."""
    log.info("Running deterministic SSN match...")

    bq.query(f"""
        CREATE OR REPLACE TABLE `{DET_MATCHES}` AS

        WITH sok_dedup AS (
          SELECT *
          FROM (
            SELECT *,
              ROW_NUMBER() OVER (
                PARTITION BY DLN
                ORDER BY _data_file_date_ DESC NULLS LAST
              ) AS rn
            FROM `{SOK_CLEAN}`
          )
          WHERE rn = 1
        )

        SELECT
            k.OwnerID, k.PropertyID,
            k.full_name_clean AS kelmar_name,
            k.BirthDT, k.CashValue,

            s.DLN,
            s.full_name_clean AS sok_name,
            s._data_file_date_,
            s.Transaction_ID, s.Transaction_Date, s.Transaction_Type,

            k.ssn_token,

            CASE
                WHEN LOWER(TRIM(k.full_name_clean)) = LOWER(TRIM(s.full_name_clean))
                    THEN 'DET_AUTO_APPROVE'
                ELSE 'DET_REVIEW'
            END AS bucket,

            CASE
                WHEN LOWER(TRIM(k.full_name_clean)) != LOWER(TRIM(s.full_name_clean))
                    THEN 'SSN_MATCH_NAME_MISMATCH'
                ELSE NULL
            END AS match_flag

        FROM `{KELMAR_CLEAN}` k
        JOIN sok_dedup s ON k.ssn_token = s.ssn_token
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{DET_MATCHES}`").to_dataframe()
    log.info("✅ Deterministic matches: %s → %s", count["n"].iloc[0], DET_MATCHES)


def identify_unmatched_kelmar(bq):
    """Kelmar records not matched by SSN → kelmar_unmatched_v1, kelmar_fuzzy_v1."""
    log.info("Identifying unmatched Kelmar records...")

    bq.query(f"""
        CREATE OR REPLACE TABLE `{KELMAR_UNMATCHED}` AS
        SELECT * FROM `{KELMAR_CLEAN}`
        WHERE ssn_token NOT IN (
            SELECT DISTINCT ssn_token FROM `{DET_MATCHES}`
        )
    """).result()

    bq.query(f"""
        CREATE OR REPLACE TABLE `{KELMAR_FUZZY}` AS
        SELECT OwnerID, PropertyID,
               first_name_clean, middle_name_clean, last_name_clean, full_name_clean,
               street_clean, city_clean, state_clean, zip_clean, BirthDT
        FROM `{KELMAR_UNMATCHED}`
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{KELMAR_FUZZY}`").to_dataframe()
    log.info("✅ Unmatched Kelmar (→ fuzzy): %s rows", count["n"].iloc[0])


# ===========================================================================
# Stage 4: Fuzzy matching
# ===========================================================================

def build_fuzzy_pool(bq):
    """Combine SSN-null + unmatched SSN-present SOK rows → sok_fuzzy_pool_v1."""
    log.info("Building fuzzy candidate pool...")

    bq.query(f"""
        CREATE OR REPLACE TABLE `{FUZZY_POOL}` AS

        SELECT CAST(DLN AS STRING) AS DLN,
               first_name_clean, middle_name_clean, last_name_clean, full_name_clean,
               street_clean, city_clean, state_clean, zip_clean,
               Date_of_Birth, NULL AS ssn_token
        FROM `{SOK_NULL_CLEAN}`

        UNION ALL

        SELECT s.DLN,
               s.first_name_clean, s.middle_name_clean, s.last_name_clean, s.full_name_clean,
               s.street_clean, s.city_clean, s.state_clean, s.zip_clean,
               s.Date_of_Birth, s.ssn_token
        FROM `{SOK_CLEAN}` s
        LEFT JOIN `{DET_MATCHES}` m ON s.ssn_token = m.ssn_token
        WHERE m.ssn_token IS NULL
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{FUZZY_POOL}`").to_dataframe()
    log.info("✅ Fuzzy pool: %s rows → %s", count["n"].iloc[0], FUZZY_POOL)


def _block_candidates_sql(block_table, join_condition):
    """Return SQL to create a candidate-pair table from a blocking join."""
    return f"""
        CREATE OR REPLACE TABLE `{block_table}` AS
        SELECT
            k.OwnerID, k.PropertyID,
            k.full_name_clean AS kelmar_name,
            k.street_clean    AS kelmar_street,
            k.city_clean      AS kelmar_city,
            k.state_clean     AS kelmar_state,
            k.zip_clean       AS kelmar_zip,
            k.BirthDT,
            s.DLN,
            s.full_name_clean AS sok_name,
            s.street_clean    AS sok_street,
            s.city_clean      AS sok_city,
            s.state_clean     AS sok_state,
            s.zip_clean       AS sok_zip,
            s.Date_of_Birth
        FROM `{KELMAR_FUZZY}` k
        JOIN `{FUZZY_POOL}` s
          ON {join_condition}
    """


def _score_and_classify(df, confidence_cap, block_name):
    """Score candidate pairs and assign buckets. Returns the DataFrame."""
    df["name_score"] = df.apply(
        lambda r: similarity(r["kelmar_name"], r["sok_name"]), axis=1
    )
    df["street_score"] = df.apply(
        lambda r: similarity(r["kelmar_street"], r["sok_street"]), axis=1
    )
    df["composite_score"] = (
        df["name_score"] * NAME_WEIGHT + df["street_score"] * STREET_WEIGHT
    )
    df["confidence_score"] = confidence_cap * (df["composite_score"] / 100)
    df["bucket"] = np.where(
        df["confidence_score"] >= AUTO_APPROVE_THRESHOLD, "FUZZY_AUTO_APPROVE",
        np.where(
            df["confidence_score"] >= REVIEW_THRESHOLD, "FUZZY_REVIEW",
            "FUZZY_REJECT",
        ),
    )
    df["block_name"]      = block_name
    df["match_stage"]     = "FUZZY"
    df["scoring_version"] = "v1"
    df["created_at"]      = datetime.utcnow()
    return df


def fuzzy_block1(bq):
    """Block 1: ZIP + Last Name + DOB → score → classify."""
    log.info("Fuzzy Block 1 (ZIP + Last + DOB)...")

    bq.query(_block_candidates_sql(
        BLOCK1_CANDIDATES,
        "k.zip_clean = s.zip_clean "
        "AND k.last_name_clean = s.last_name_clean "
        "AND SAFE.PARSE_DATE('%m/%d/%Y', k.BirthDT) = s.Date_of_Birth",
    )).result()

    df = bq.query(f"SELECT * FROM `{BLOCK1_CANDIDATES}`").to_dataframe()
    log.info("  candidates: %d", len(df))

    df = _score_and_classify(df, BLOCK1_CONFIDENCE_CAP, "BLOCK1_ZIP_LAST_DOB")

    df.to_gbq(BLOCK1_CLASSIFIED, project_id=PROJECT_ID, if_exists="replace")
    log.info("✅ Block 1 classified: %d rows", len(df))
    log.info("  %s", df["bucket"].value_counts().to_dict())


def fuzzy_block2(bq):
    """Block 2: ZIP + Last Name only → score → classify.

    Confidence capped at 85 — can never reach FUZZY_AUTO_APPROVE by design.
    """
    log.info("Fuzzy Block 2 (ZIP + Last)...")

    bq.query(_block_candidates_sql(
        BLOCK2_CANDIDATES,
        "k.zip_clean = s.zip_clean "
        "AND k.last_name_clean = s.last_name_clean",
    )).result()

    df = bq.query(f"SELECT * FROM `{BLOCK2_CANDIDATES}`").to_dataframe()
    log.info("  candidates: %d", len(df))

    df = _score_and_classify(df, BLOCK2_CONFIDENCE_CAP, "BLOCK2_ZIP_LAST")

    df.to_gbq(BLOCK2_CLASSIFIED, project_id=PROJECT_ID, if_exists="replace")
    log.info("✅ Block 2 classified: %d rows", len(df))
    log.info("  %s", df["bucket"].value_counts().to_dict())


# ===========================================================================
# Stage 5: Combine, dedupe, enrich
# ===========================================================================

def assemble_review_table(bq):
    """Merge deterministic + fuzzy results → treasury_match_review_v2."""
    log.info("Assembling final review table...")

    # --- DLN → latest Transaction_ID mapping ---
    dln_map = bq.query(f"""
        SELECT
          CAST(DLN AS STRING) AS DLN,
          NULLIF(CAST(Transaction_ID AS STRING), '') AS Transaction_ID,
          _data_file_date_
        FROM `{SOK_TOKENIZED}`
        WHERE DLN IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY CAST(DLN AS STRING)
          ORDER BY
            _data_file_date_ DESC,
            SAFE_CAST(Transaction_Date AS TIMESTAMP) DESC,
            IF(NULLIF(CAST(Transaction_ID AS STRING), '') IS NULL, 1, 0) ASC
        ) = 1
    """).to_dataframe()

    # --- Deterministic: join back for full fields (SOK deduped by DLN) ---
    det = bq.query(f"""
        WITH sok_latest AS (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY DLN ORDER BY _data_file_date_ DESC NULLS LAST
          ) AS rn
          FROM `{SOK_CLEAN}`
        )
        SELECT
            d.OwnerID, d.PropertyID, CAST(d.DLN AS STRING) AS DLN,
            k.full_name_clean AS kelmar_name,  k.street_clean AS kelmar_street,
            k.city_clean AS kelmar_city, k.state_clean AS kelmar_state,
            k.zip_clean AS kelmar_zip, k.BirthDT,
            sc.full_name_clean AS sok_name, sc.street_clean AS sok_street,
            sc.city_clean AS sok_city, sc.state_clean AS sok_state,
            sc.zip_clean AS sok_zip, sc.Date_of_Birth,
            'DETERMINISTIC_SSN' AS technique,
            CASE WHEN LOWER(TRIM(k.full_name_clean)) != LOWER(TRIM(sc.full_name_clean))
                 THEN 'SSN_MATCH_NAME_MISMATCH' ELSE NULL
            END AS match_flag
        FROM `{DET_MATCHES}` d
        JOIN `{KELMAR_CLEAN}` k
          ON CAST(d.OwnerID AS INT64) = CAST(k.OwnerID AS INT64)
         AND CAST(d.PropertyID AS INT64) = CAST(k.PropertyID AS INT64)
        JOIN sok_latest sc
          ON CAST(d.DLN AS STRING) = CAST(sc.DLN AS STRING) AND sc.rn = 1
    """).to_dataframe()

    # Score deterministic
    det["name_score"] = det.apply(
        lambda r: similarity(r["kelmar_name"], r["sok_name"]), axis=1
    )
    det["street_score"] = det.apply(
        lambda r: similarity(r["kelmar_street"], r["sok_street"]), axis=1
    )
    det["composite_score"] = det["name_score"] * NAME_WEIGHT + det["street_score"] * STREET_WEIGHT
    det["confidence_score"] = det["composite_score"]  # 100% ceiling for SSN match

    det["bucket"] = np.where(
        det["match_flag"].isna(), "DET_AUTO_APPROVE",
        np.where(det["name_score"] >= 90, "DET_REVIEW_MINOR",
                 np.where(det["name_score"] >= 80, "DET_REVIEW_MODERATE",
                          "DET_REVIEW_MISMATCH")),
    )
    det = det.merge(dln_map, on="DLN", how="left")
    log.info("  deterministic rows: %d", len(det))

    # --- Load fuzzy blocks ---
    b1 = bq.query(f"SELECT * FROM `{BLOCK1_CLASSIFIED}`").to_dataframe()
    b2 = bq.query(f"SELECT * FROM `{BLOCK2_CLASSIFIED}`").to_dataframe()

    b1["DLN"] = b1["DLN"].astype("string")
    b2["DLN"] = b2["DLN"].astype("string")

    b1["technique"] = "ZIP+LAST+DOB_STRONG_BLOCK"
    b1["confidence_score"] = BLOCK1_CONFIDENCE_CAP * (b1["composite_score"] / 100)

    b2["technique"] = "ZIP+LAST_FUZZY_BLOCK"
    b2["confidence_score"] = BLOCK2_CONFIDENCE_CAP * (b2["composite_score"] / 100)

    b1 = b1.merge(dln_map, on="DLN", how="left")
    b2 = b2.merge(dln_map, on="DLN", how="left")
    b1["match_flag"] = None
    b2["match_flag"] = None

    # --- Standardize columns ---
    cols = [
        "OwnerID", "PropertyID", "DLN",
        "Transaction_ID", "_data_file_date_",
        "kelmar_name", "kelmar_street", "kelmar_city", "kelmar_state", "kelmar_zip", "BirthDT",
        "sok_name", "sok_street", "sok_city", "sok_state", "sok_zip", "Date_of_Birth",
        "technique", "name_score", "street_score", "confidence_score", "composite_score",
        "bucket", "match_flag",
    ]

    for label, df in [("det", det), ("block1", b1), ("block2", b2)]:
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{label} missing columns: {missing}")

    # --- Combine + dedupe ---
    combined = pd.concat([det[cols], b1[cols], b2[cols]], ignore_index=True)
    combined = (
        combined.sort_values("confidence_score", ascending=False)
        .drop_duplicates(subset=["OwnerID", "PropertyID", "DLN"])
    )

    null_buckets = combined["bucket"].isna().sum()
    assert null_buckets == 0, f"❌ Found {null_buckets} null buckets!"

    combined.to_gbq(REVIEW_TABLE, project_id=PROJECT_ID, if_exists="replace")
    log.info("✅ Review table: %d rows, 0 null buckets → %s", len(combined), REVIEW_TABLE)
    log.info("  %s", combined["bucket"].value_counts().to_dict())


def enrich_review_table(bq):
    """Add CashValue + Deceased flag → treasury_match_review_v3."""
    log.info("Enriching with CashValue and Deceased flag...")

    row_count = bq.query(f"SELECT COUNT(*) AS n FROM `{REVIEW_TABLE}`").to_dataframe()["n"].iloc[0]
    if row_count == 0:
        log.info("Review table is empty — skipping enrichment")
        return

    bq.query(f"""
        CREATE OR REPLACE TABLE `{REVIEW_ENRICHED}` AS
        WITH deceased_person AS (
          SELECT CAST(DLN AS STRING) AS DLN, ANY_VALUE(Deceased) AS Deceased
          FROM `{SOK_STAGING}` WHERE DLN IS NOT NULL
          GROUP BY DLN
        )
        SELECT t.*, k.CashValue, d.Deceased
        FROM `{REVIEW_TABLE}` t
        LEFT JOIN `{KELMAR_CLEAN}` k
          ON CAST(t.OwnerID AS INT64) = k.OwnerID AND CAST(t.PropertyID AS INT64) = k.PropertyID
        LEFT JOIN deceased_person d ON CAST(t.DLN AS STRING) = d.DLN
    """).result()

    log.info("✅ Enriched → %s", REVIEW_ENRICHED)


def cap_and_deliver(bq):
    """Cap fuzzy to top-1 per property, add eligibility → capped_v2."""
    log.info("Capping candidates and building delivery table...")

    row_count = bq.query(f"SELECT COUNT(*) AS n FROM `{REVIEW_TABLE}`").to_dataframe()["n"].iloc[0]
    if row_count == 0:
        log.info("No matches to cap — skipping")
        return

    bq.query(f"""
        CREATE OR REPLACE TABLE `{REVIEW_CAPPED_V1}` AS
        WITH ranked AS (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY OwnerID, PropertyID
            ORDER BY confidence_score DESC
          ) AS rank_within_property
          FROM `{REVIEW_TABLE}`
        )
        SELECT * FROM ranked
        WHERE technique = 'DETERMINISTIC_SSN' OR rank_within_property = 1
    """).result()

    bq.query(f"""
        CREATE OR REPLACE TABLE `{REVIEW_CAPPED_V2}` AS
        WITH enriched AS (
          SELECT *,
            CASE WHEN Deceased = TRUE THEN 'INELIGIBLE_DECEASED'
                 ELSE 'ELIGIBLE'
            END AS Eligibility_Flag,
            ROW_NUMBER() OVER (
              PARTITION BY OwnerID, PropertyID
              ORDER BY confidence_score DESC
            ) AS property_rank
          FROM `{REVIEW_ENRICHED}`
        )
        SELECT * FROM enriched
        WHERE technique = 'DETERMINISTIC_SSN' OR property_rank = 1
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{REVIEW_CAPPED_V2}`").to_dataframe()
    log.info("✅ Delivery table: %s rows → %s", count["n"].iloc[0], REVIEW_CAPPED_V2)


def build_unmatched_table(bq):
    """Kelmar properties with no match at all → treasury_unmatched_v1."""
    log.info("Building unmatched table...")

    row_count = bq.query(f"SELECT COUNT(*) AS n FROM `{REVIEW_TABLE}`").to_dataframe()["n"].iloc[0]
    if row_count == 0:
        log.info("No matches — all Kelmar records are unmatched")
        bq.query(f"""
            CREATE OR REPLACE TABLE `{UNMATCHED_TABLE}` AS
            SELECT OwnerID, PropertyID,
                   full_name_clean AS kelmar_name,
                   street_clean AS kelmar_street, city_clean AS kelmar_city,
                   state_clean AS kelmar_state, zip_clean AS kelmar_zip,
                   BirthDT
            FROM `{KELMAR_CLEAN}`
        """).result()
        count = bq.query(f"SELECT COUNT(*) AS n FROM `{UNMATCHED_TABLE}`").to_dataframe()
        log.info("Unmatched: %s rows (all Kelmar)", count["n"].iloc[0])
        return

    bq.query(f"""
        CREATE OR REPLACE TABLE `{UNMATCHED_TABLE}` AS
        SELECT
            k.OwnerID, k.PropertyID,
            k.full_name_clean AS kelmar_name,
            k.street_clean AS kelmar_street, k.city_clean AS kelmar_city,
            k.state_clean AS kelmar_state, k.zip_clean AS kelmar_zip,
            k.BirthDT
        FROM `{KELMAR_CLEAN}` k
        LEFT JOIN `{REVIEW_TABLE}` r
          ON CAST(k.OwnerID AS INT64) = CAST(r.OwnerID AS INT64)
         AND CAST(k.PropertyID AS INT64) = CAST(r.PropertyID AS INT64)
        WHERE r.OwnerID IS NULL
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{UNMATCHED_TABLE}`").to_dataframe()
    log.info("✅ Unmatched: %s rows → %s", count["n"].iloc[0], UNMATCHED_TABLE)


# ===========================================================================
# Stage 6: Publish MPI output
# ===========================================================================

def publish_mpi_output(bq):
    """Publish final results to BigQuery and GCS.

    BigQuery: OK_OST_OMES_OUTBOUND_DataMatch (overwritten each run)
    GCS:      OK_OST_OMES_OUTBOUND_DataMatch_MDDYY.csv (new file each run, never overwritten)
    """
    log.info("Publishing MPI output...")

    row_count = bq.query(f"SELECT COUNT(*) AS n FROM `{REVIEW_TABLE}`").to_dataframe()["n"].iloc[0]
    if row_count == 0:
        log.info("No matches to publish — skipping MPI output")
        return

    # 1) BigQuery — overwrite the single delivery table
    bq.query(f"""
        CREATE OR REPLACE TABLE `{MPI_OUTPUT_TABLE}` AS
        SELECT * EXCEPT(DLN) FROM `{REVIEW_CAPPED_V2}`
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{MPI_OUTPUT_TABLE}`").to_dataframe()
    log.info("✅ BQ output: %s rows → %s", count["n"].iloc[0], MPI_OUTPUT_TABLE)

    # 2) GCS — dated copy (MDDYY format, never overwritten)
    now = datetime.utcnow()
    date_suffix = f"{now.month}{now.strftime('%d%y')}"
    gcs_filename = f"OK_OST_OMES_OUTBOUND_DataMatch_{date_suffix}.csv"
    gcs_path = f"{GCS_MPI_PATH}/{gcs_filename}"

    df = bq.query(f"SELECT * FROM `{MPI_OUTPUT_TABLE}`").to_dataframe()
    csv_data = df.to_csv(index=False)

    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(GCS_MPI_BUCKET)
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(csv_data, content_type="text/csv")
    log.info("✅ GCS output: gs://%s/%s", GCS_MPI_BUCKET, gcs_path)


# ===========================================================================
# Entry point
# ===========================================================================

def run_pipeline():
    """Execute the full CitizenMatch pipeline end-to-end."""
    start = time.time()
    log.info("=" * 60)
    log.info("CitizenMatch pipeline starting")
    log.info("=" * 60)

    bq  = _get_bq_client()
    dlp = _get_dlp_client()

    # Stage 1: Tokenize
    tokenize_kelmar(bq, dlp)
    tokenize_sok(bq, dlp)

    # Check if SOK tokenization is still in progress
    # If checkpoint exists, SOK is not fully loaded — skip matching stages
    CHECKPOINT_TABLE = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_tokenization_checkpoint"
    try:
        cp_count = bq.query(
            f"SELECT COUNT(*) AS n FROM `{CHECKPOINT_TABLE}`"
        ).to_dataframe()["n"].iloc[0]
        if cp_count > 0:
            elapsed = time.time() - start
            log.info("=" * 60)
            log.info("SOK tokenization still in progress (%d rows so far)",
                     int(bq.query(f"SELECT COUNT(*) AS n FROM `{SOK_TOKENIZED}`").to_dataframe()["n"].iloc[0]))
            log.info("Trigger the pipeline again to continue tokenization")
            log.info("Pipeline paused after %.1f seconds", elapsed)
            log.info("=" * 60)
            return
    except Exception:
        pass  # No checkpoint table = tokenization complete

    # Create/update BigQuery cleaning UDFs (idempotent)
    dataset_ref = f"{PROJECT_ID}.{OUTPUT_DATASET}"
    create_cleaning_udfs(bq, dataset_ref)

    # Stage 2: Clean (runs entirely in BigQuery — zero memory footprint)
    clean_kelmar_table(bq)
    clean_sok_table(bq)
    clean_sok_null_table(bq)

    # Stage 3: Deterministic match
    deterministic_match(bq)
    identify_unmatched_kelmar(bq)

    # Stage 4: Fuzzy match
    build_fuzzy_pool(bq)
    fuzzy_block1(bq)
    fuzzy_block2(bq)

    # Stage 5: Combine + deliver
    assemble_review_table(bq)
    enrich_review_table(bq)
    cap_and_deliver(bq)
    build_unmatched_table(bq)

    # Stage 6: Publish MPI output
    publish_mpi_output(bq)

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("Pipeline complete in %.1f seconds", elapsed)
    log.info("=" * 60)
