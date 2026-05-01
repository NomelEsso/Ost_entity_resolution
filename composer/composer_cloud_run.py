"""
Composer DAG: End-to-end CitizenMatch pipeline (fully automated).

Flow:
  1. Trigger DE's ingestion DAG (GCS → BigQuery)
  2. Fire Cloud Run calls (don't wait for response) and poll BigQuery
     until SOK is fully tokenized and matching is complete
  3. No manual intervention required

Strategy: Cloud Run continues processing even after the HTTP client
disconnects. We fire the call with a tiny timeout, then poll BigQuery
tables to track progress. This avoids all HTTP connection issues.

Initial load (~6.5M SOK rows): ~7 iterations, ~2-3 hours total
Monthly runs (~100K new SOK rows): 1 iteration, ~15 minutes

Schedule: None (manual) for testing. Change to "0 7 1 * *" for monthly.
"""

import time
from datetime import datetime, timedelta

import requests
from google.oauth2 import id_token
from google.auth.transport.requests import Request
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLOUD_RUN_URL = "https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app"
DE_DAG_ID     = "gcs_to_bq_unclaimed_property_ingestion"

MAX_ITERATIONS   = 10    # Max Cloud Run calls (10 * 1M = 10M rows capacity)
POLL_INTERVAL    = 120   # Seconds between BigQuery polls
MAX_WAIT_PER_RUN = 4200  # Max seconds to wait for one Cloud Run run (70 min)

# ---------------------------------------------------------------------------
# SQL queries for polling
# ---------------------------------------------------------------------------
CHECKPOINT_SQL = """
    SELECT COUNT(*) FROM
    `aw-ost-property-np.citizen_match.sok_tokenization_checkpoint`
"""
OUTPUT_SQL = """
    SELECT COUNT(*) FROM
    `aw-ost-property-np.citizen_mpi_result.OK_OST_OMES_OUTBOUND_DataMatch`
"""
TOKENIZED_SQL = """
    SELECT COUNT(*) FROM
    `aw-ost-property-np.citizen_match.sok_staging_dataset_tokenized_v2`
"""

# ---------------------------------------------------------------------------
# Default args
# ---------------------------------------------------------------------------
default_args = {
    "owner": "Nomel",
    "retries": 3,
    "retry_delay": timedelta(minutes=2),
}

# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------
with DAG(
    dag_id="citizenmatch_pipeline",
    description="Ingest Kelmar → tokenize SOK → match → deliver (fully automated)",
    start_date=datetime(2025, 1, 1),
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["citizenmatch", "entity-resolution"],
) as dag:

    trigger_ingestion = TriggerDagRunOperator(
        task_id="trigger_kelmar_ingestion",
        trigger_dag_id=DE_DAG_ID,
        wait_for_completion=True,
        poke_interval=30,
        execution_timeout=timedelta(minutes=30),
    )

    def tokenize_and_match():
        hook = BigQueryHook(
            gcp_conn_id="google_cloud_default",
            use_legacy_sql=False,
        )

        def _bq_count(sql):
            """Run a COUNT query, return 0 if table doesn't exist."""
            try:
                rows = hook.get_records(sql)
                return rows[0][0] if rows else 0
            except Exception:
                return 0

        def _fire_cloud_run():
            """POST to Cloud Run with tiny timeout. Don't wait for response.

            Cloud Run continues processing even after client disconnects.
            We track progress by polling BigQuery instead.
            """
            try:
                token = id_token.fetch_id_token(Request(), CLOUD_RUN_URL)
                requests.post(
                    CLOUD_RUN_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=(60, 10),  # 60s connect (cold start), 10s read
                )
                print("Cloud Run call returned (fast response)")
            except requests.exceptions.ReadTimeout:
                print("Cloud Run call fired (processing in background)")
            except requests.exceptions.ConnectionError:
                print("Cloud Run connection error (will retry)")
            except Exception as e:
                print(f"Cloud Run call error: {e} (will check progress)")

        def _wait_for_progress(prev_tokenized):
            """Poll BigQuery until Cloud Run finishes its current batch.

            Returns: ("COMPLETE", "NEED_MORE", or "STALE")
            """
            stale_count = 0

            for _ in range(MAX_WAIT_PER_RUN // POLL_INTERVAL):
                time.sleep(POLL_INTERVAL)

                checkpoint = _bq_count(CHECKPOINT_SQL)
                output = _bq_count(OUTPUT_SQL)
                tokenized = _bq_count(TOKENIZED_SQL)

                print(f"  Poll: tokenized={tokenized}, "
                      f"checkpoint={'yes' if checkpoint else 'no'}, "
                      f"output={output}")

                # Matching finished — pipeline complete
                if output > 0:
                    return "COMPLETE"

                # Checkpoint gone, no output yet — matching might be running
                if checkpoint == 0:
                    # Wait extra for matching stages to finish
                    print("  Checkpoint gone — waiting for matching...")
                    for _ in range(10):  # Up to 20 more minutes
                        time.sleep(120)
                        output = _bq_count(OUTPUT_SQL)
                        if output > 0:
                            return "COMPLETE"
                    # Matching didn't produce output — trigger again
                    return "NEED_MORE"

                # Checkpoint exists — check if tokenized count grew
                if tokenized > prev_tokenized:
                    stale_count = 0
                    prev_tokenized = tokenized
                else:
                    stale_count += 1

                # If count stopped growing for 6 polls (12 min),
                # Cloud Run finished this batch
                if stale_count >= 6:
                    return "NEED_MORE"

            return "NEED_MORE"

        # =================================================================
        # Main loop
        # =================================================================
        for i in range(1, MAX_ITERATIONS + 1):
            checkpoint = _bq_count(CHECKPOINT_SQL)
            output = _bq_count(OUTPUT_SQL)
            tokenized = _bq_count(TOKENIZED_SQL)

            print(f"=== Iteration {i}/{MAX_ITERATIONS} ===")
            print(f"SOK tokenized: {tokenized}, "
                  f"checkpoint: {'yes' if checkpoint else 'no'}, "
                  f"output: {output}")

            # Already done?
            if output > 0 and checkpoint == 0:
                print(f"Pipeline already complete! Output: {output} rows")
                return

            # Fire Cloud Run (don't wait for response)
            print("Firing Cloud Run...")
            _fire_cloud_run()

            # Brief pause to let Cloud Run start
            time.sleep(30)

            # Poll BigQuery until this batch finishes
            result = _wait_for_progress(tokenized)

            if result == "COMPLETE":
                final_output = _bq_count(OUTPUT_SQL)
                print(f"Pipeline complete after {i} iteration(s)! "
                      f"Output: {final_output} rows")
                return

            print(f"Need more processing — continuing to iteration {i + 1}")

        # If we get here, something is wrong
        final_tok = _bq_count(TOKENIZED_SQL)
        final_cp = _bq_count(CHECKPOINT_SQL)
        final_out = _bq_count(OUTPUT_SQL)
        raise RuntimeError(
            f"Pipeline did not complete in {MAX_ITERATIONS} iterations. "
            f"Tokenized: {final_tok}, Checkpoint: {'yes' if final_cp else 'no'}, "
            f"Output: {final_out}"
        )

    run_pipeline = PythonOperator(
        task_id="tokenize_and_match",
        python_callable=tokenize_and_match,
        execution_timeout=timedelta(hours=12),
    )

    trigger_ingestion >> run_pipeline
