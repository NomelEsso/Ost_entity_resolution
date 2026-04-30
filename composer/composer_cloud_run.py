"""
Composer DAG: End-to-end CitizenMatch pipeline (fully automated).

Flow:
  1. Trigger DE's ingestion DAG (GCS → BigQuery)
  2. Call Cloud Run repeatedly until SOK is fully tokenized
     - Each call processes up to 1M SOK rows and saves checkpoint
     - When checkpoint is gone, the final call runs full matching pipeline
  3. No manual intervention required

Initial load (~6.5M SOK rows): ~7 iterations, ~5 hours total
Monthly runs (~100K new SOK rows): 1 iteration, ~15 minutes

Schedule: None (manual) for testing → "0 7 1 * *" for monthly production
"""

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
CLOUD_RUN_URL  = "https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app"
DE_DAG_ID      = "gcs_to_bq_unclaimed_property_ingestion"
CHECKPOINT_SQL = (
    "SELECT COUNT(*) FROM "
    "`aw-ost-property-np.citizen_match.sok_tokenization_checkpoint`"
)
MAX_ITERATIONS = 10  # Safety limit: 10 * 1M rows = 10M (more than enough for 6.5M)

# ---------------------------------------------------------------------------
# Default args
# ---------------------------------------------------------------------------
default_args = {
    "owner": "Nomel",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------
with DAG(
    dag_id="citizenmatch_pipeline",
    description="Ingest Kelmar → tokenize SOK → match → deliver (fully automated)",
    start_date=datetime(2025, 1, 1),
    schedule_interval=None,          # manual for testing — "0 7 1 * *" for monthly
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["citizenmatch", "entity-resolution"],
) as dag:

    # Step 1: Trigger DE's ingestion DAG
    trigger_ingestion = TriggerDagRunOperator(
        task_id="trigger_kelmar_ingestion",
        trigger_dag_id=DE_DAG_ID,
        wait_for_completion=True,
        poke_interval=30,
        execution_timeout=timedelta(minutes=30),
    )

    # Step 2: Call Cloud Run in a loop until SOK is fully tokenized + matching is done
    def tokenize_and_match():
        """Repeatedly call Cloud Run until SOK tokenization completes.

        Each call:
          - Tokenizes up to 1M SOK rows (saves checkpoint)
          - If checkpoint exists → returns success, we call again
          - If no checkpoint → runs full matching pipeline, we're done

        The final iteration runs: clean → match → score → output.
        """
        hook = BigQueryHook(
            gcp_conn_id="google_cloud_default",
            use_legacy_sql=False,
        )

        for i in range(1, MAX_ITERATIONS + 1):
            print(f"--- Iteration {i}/{MAX_ITERATIONS} ---")

            # Call Cloud Run
            token = id_token.fetch_id_token(Request(), CLOUD_RUN_URL)
            resp = requests.post(
                CLOUD_RUN_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=3600,  # 1 hour max per call
            )

            print(f"Status: {resp.status_code}")
            print(f"Response: {resp.text}")

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Pipeline failed on iteration {i}: "
                    f"{resp.status_code} — {resp.text}"
                )

            # Check if SOK tokenization is still in progress
            try:
                rows = hook.get_records(CHECKPOINT_SQL)
                checkpoint_count = rows[0][0] if rows else 0
            except Exception:
                checkpoint_count = 0  # Table doesn't exist = done

            if checkpoint_count == 0:
                print(f"✅ Pipeline complete after {i} iteration(s)")
                return

            print(f"SOK tokenization in progress — triggering next batch...")

        raise RuntimeError(
            f"SOK tokenization did not complete within {MAX_ITERATIONS} iterations. "
            f"Check sok_tokenization_checkpoint table."
        )

    run_pipeline = PythonOperator(
        task_id="tokenize_and_match",
        python_callable=tokenize_and_match,
        execution_timeout=timedelta(hours=12),
    )

    trigger_ingestion >> run_pipeline
