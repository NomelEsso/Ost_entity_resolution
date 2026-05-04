"""
Composer DAG: End-to-end CitizenMatch pipeline (fully automated).

Flow:
  1. Trigger DE's ingestion DAG (GCS → BigQuery)
  2. Call Cloud Run with full timeout (keep connection alive)
  3. If response lost, check BigQuery and retry
  4. Trigger audit pipeline when complete

IMPORTANT: Cloud Run terminates containers when no active request exists.
We MUST keep the HTTP connection alive for the full duration. Fire-and-forget
does NOT work with Cloud Run.

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

MAX_ITERATIONS = 10

# ---------------------------------------------------------------------------
# Default args — 3 retries with 2 min delay
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
        """Call Cloud Run repeatedly until pipeline completes.

        Each call keeps the HTTP connection alive for up to 1 hour.
        If the connection drops (ReadTimeout), we check BigQuery to see
        if Cloud Run actually finished, then retry if needed.

        Cloud Run processes up to 1M SOK rows per call. When all SOK rows
        are tokenized, the final call runs matching and produces output.
        """
        hook = BigQueryHook(
            gcp_conn_id="google_cloud_default",
            use_legacy_sql=False,
            location="us-central1",
        )

        def _bq_count(sql):
            """Run a COUNT query, return 0 if table doesn't exist."""
            try:
                rows = hook.get_records(sql)
                return rows[0][0] if rows else 0
            except Exception:
                return 0

        def _get_status():
            """Check pipeline status from BigQuery tables."""
            tokenized = _bq_count(
                "SELECT COUNT(*) FROM "
                "`aw-ost-property-np.citizen_match.sok_staging_dataset_tokenized_v2`"
            )
            checkpoint = _bq_count(
                "SELECT COUNT(*) FROM "
                "`aw-ost-property-np.citizen_match.sok_tokenization_checkpoint`"
            )
            output = _bq_count(
                "SELECT COUNT(*) FROM "
                "`aw-ost-property-np.citizen_mpi_result.OK_OST_OMES_OUTBOUND_DataMatch`"
            )
            return tokenized, checkpoint, output

        for i in range(1, MAX_ITERATIONS + 1):
            tokenized, checkpoint, output = _get_status()

            print(f"=== Iteration {i}/{MAX_ITERATIONS} ===")
            print(f"SOK tokenized: {tokenized}, "
                  f"checkpoint: {'yes' if checkpoint else 'no'}, "
                  f"output: {output}")

            # Already done?
            if output > 0 and checkpoint == 0:
                print(f"Pipeline complete! Output: {output} rows")
                return

            # Call Cloud Run — keep connection alive
            print("Calling Cloud Run (keeping connection alive)...")
            token = id_token.fetch_id_token(Request(), CLOUD_RUN_URL)

            try:
                resp = requests.post(
                    CLOUD_RUN_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=3600,  # 1 hour — must keep alive
                )
                print(f"Response: {resp.status_code} — {resp.text}")

                if resp.status_code == 200:
                    # Cloud Run finished — check what happened
                    time.sleep(10)
                    tokenized, checkpoint, output = _get_status()

                    if output > 0 and checkpoint == 0:
                        print(f"Pipeline complete! Output: {output} rows")
                        return

                    if checkpoint > 0:
                        print("More tokenization needed — continuing...")
                        continue

                    if checkpoint == 0 and output == 0:
                        print("Tokenization done, matching may need another trigger...")
                        continue
                else:
                    print(f"Non-200 response — checking BigQuery...")
                    time.sleep(30)
                    tokenized, checkpoint, output = _get_status()
                    if output > 0:
                        print(f"Pipeline complete despite error! Output: {output}")
                        return
                    # Will retry in next iteration

            except requests.exceptions.ReadTimeout:
                # Connection dropped but Cloud Run may have finished
                print("ReadTimeout — checking BigQuery for progress...")
                time.sleep(30)
                new_tokenized, checkpoint, output = _get_status()

                print(f"After timeout: tokenized={new_tokenized}, "
                      f"checkpoint={'yes' if checkpoint else 'no'}, "
                      f"output={output}")

                if output > 0 and checkpoint == 0:
                    print(f"Pipeline complete! Output: {output} rows")
                    return

                if new_tokenized > tokenized:
                    print("Progress detected — continuing...")
                    continue

                print("Will retry...")
                continue

            except requests.exceptions.ConnectionError as e:
                print(f"Connection error: {e}")
                time.sleep(60)
                tokenized, checkpoint, output = _get_status()
                if output > 0:
                    print(f"Pipeline complete! Output: {output}")
                    return
                print("Will retry...")
                continue

        # Exhausted iterations
        final_tok, final_cp, final_out = _get_status()
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

    # Step 3: Trigger audit pipeline after matching completes
    trigger_audit = TriggerDagRunOperator(
        task_id="trigger_audit_pipeline",
        trigger_dag_id="kelmar_outbound",
        wait_for_completion=False,
        execution_timeout=timedelta(minutes=5),
    )

    trigger_ingestion >> run_pipeline >> trigger_audit
