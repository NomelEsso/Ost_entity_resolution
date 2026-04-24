"""
CitizenMatch pipeline — core matching logic.

Stages:
  1. Tokenize SSNs (Kelmar + SOK) via Cloud DLP
  2. Clean both datasets
  3. Deterministic SSN matching
  4. Fuzzy matching (Block 1: ZIP+Last+DOB, Block 2: ZIP+Last)
  5. Combine, dedupe, enrich, cap candidates
  6. Produce delivery + unmatched tables

Called by main.py via run_pipeline().
"""

import logging
import time
from datetime import datetime

import numpy as np
import pandas as pd
from google.cloud import bigquery, dlp_v2
import google.auth
from google.auth import impersonated_credentials as ic

from config import (
    PROJECT_ID, LOCATION, IMPERSONATE_SA,
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
    BLOCK1_CONFIDENCE_CAP, BLOCK2_CONFIDENCE_CAP,
    AUTO_APPROVE_THRESHOLD, REVIEW_THRESHOLD,
    NAME_WEIGHT, STREET_WEIGHT,
)
from cleaners import (
    clean_sok_names, clean_sok_address,
    clean_kelmar_names, clean_kelmar_address,
    similarity,
)

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
    """Tokenize all SOK SSNs (chunked) → sok_staging_dataset_tokenized_v2."""
    log.info("Tokenizing SOK SSNs (chunked)...")

    def _fetch_chunk(last_txn, last_dln, last_file, last_ssn):
        where = "WHERE SSN IS NOT NULL AND DLN IS NOT NULL"
        if last_txn is not None:
            where += f"""
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
            {where}
            ORDER BY
              COALESCE(CAST(Transaction_ID AS STRING), ''),
              CAST(DLN AS STRING),
              COALESCE(CAST(_data_file_date_ AS STRING), ''),
              CAST(SSN AS STRING)
            LIMIT {BQ_CHUNK_ROWS}
        """).to_dataframe(create_bqstorage_client=True)

    last_txn = None
    last_dln = last_file = last_ssn = ""
    first_write = True
    total = 0

    while True:
        chunk = _fetch_chunk(last_txn, last_dln, last_file, last_ssn)
        if chunk.empty:
            break

        chunk["SSN"] = chunk["SSN"].astype("string").str.strip()
        chunk["ssn_token"] = _tokenize_batch(dlp, chunk["SSN"].tolist())
        chunk["ssn_last4"] = chunk["SSN"].str.replace(r"\D", "", regex=True).str[-4:]

        # Advance cursor BEFORE dropping SSN
        last_txn  = chunk["Transaction_ID"].iloc[-1]
        last_dln  = chunk["DLN"].iloc[-1]
        last_file = chunk["_data_file_date_"].iloc[-1]
        last_ssn  = chunk["SSN"].iloc[-1]

        chunk = chunk.drop(columns=["SSN"])  # ⛔ never persist raw SSN

        _write_to_bq(bq, chunk, SOK_TOKENIZED, truncate=first_write)
        first_write = False
        total += len(chunk)
        log.info("  wrote %d rows (total: %d)", len(chunk), total)

        # Test mode: stop after SOK_MAX_ROWS (0 = no limit)
        if SOK_MAX_ROWS and total >= SOK_MAX_ROWS:
            log.info("  test limit reached (%d rows), stopping SOK tokenization", SOK_MAX_ROWS)
            break

    log.info("✅ SOK tokenized: %d rows → %s", total, SOK_TOKENIZED)


# ===========================================================================
# Stage 2: Cleaning
# ===========================================================================

def clean_sok_table(bq):
    """Clean SOK tokenized (SSN-present) rows → sok_clean_v1."""
    log.info("Cleaning SOK (SSN-present)...")
    df = bq.query(f"SELECT * FROM `{SOK_TOKENIZED}`").to_dataframe(
        create_bqstorage_client=True
    )
    df = clean_sok_names(df)
    df = clean_sok_address(df)
    _write_to_bq(bq, df, SOK_CLEAN)
    log.info("✅ SOK cleaned: %d rows → %s", len(df), SOK_CLEAN)


def clean_kelmar_table(bq):
    """Clean Kelmar tokenized rows → kelmar_clean_v1."""
    log.info("Cleaning Kelmar...")
    df = bq.query(f"SELECT * FROM `{KELMAR_TOKENIZED}`").to_dataframe()
    df = clean_kelmar_names(df)
    df = clean_kelmar_address(df)
    _write_to_bq(bq, df, KELMAR_CLEAN)
    log.info("✅ Kelmar cleaned: %d rows → %s", len(df), KELMAR_CLEAN)


def clean_sok_null_table(bq):
    """Extract and clean SOK rows where SSN IS NULL → sok_ssn_null_clean_v1."""
    log.info("Cleaning SOK (SSN-null)...")

    # Extract SSN-null subset with explicit column types
    # (SELECT * would bring raw types that don't match the tokenized pipeline)
    limit_clause = f"LIMIT {SOK_MAX_ROWS}" if SOK_MAX_ROWS else ""
    bq.query(f"""
        CREATE OR REPLACE TABLE `{SOK_NULL_CLEAN}` AS
        SELECT
          COALESCE(CAST(Transaction_ID AS STRING), '') AS Transaction_ID,
          COALESCE(CAST(_data_file_date_ AS STRING), '') AS _data_file_date_,
          Transaction_Date,
          Transaction_Type,
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
          Mailing_Address_City, Mailing_Address_State, Mailing_Address_Zip
        FROM `{SOK_STAGING}`
        WHERE SSN IS NULL
          AND DLN IS NOT NULL
        {limit_clause}
    """).result()

    # Load, clean, write back
    df = bq.query(f"SELECT * FROM `{SOK_NULL_CLEAN}`").to_dataframe(
        create_bqstorage_client=True
    )
    df = clean_sok_names(df)
    df = clean_sok_address(df)
    _write_to_bq(bq, df, SOK_NULL_CLEAN)
    log.info("✅ SOK SSN-null cleaned: %d rows → %s", len(df), SOK_NULL_CLEAN)


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
        "AND k.BirthDT = s.Date_of_Birth",
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
          ON d.OwnerID = k.OwnerID AND d.PropertyID = k.PropertyID
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

    # Skip if review table is empty (avoids type errors on empty tables)
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

    # Capped v1 (without enrichment)
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

    # Capped v2 (enriched + eligibility flag)
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
          ON k.OwnerID = r.OwnerID AND k.PropertyID = r.PropertyID
        WHERE r.OwnerID IS NULL
    """).result()

    count = bq.query(f"SELECT COUNT(*) AS n FROM `{UNMATCHED_TABLE}`").to_dataframe()
    log.info("✅ Unmatched: %s rows → %s", count["n"].iloc[0], UNMATCHED_TABLE)


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

    # Stage 2: Clean
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

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("Pipeline complete in %.1f seconds", elapsed)
    log.info("=" * 60)
