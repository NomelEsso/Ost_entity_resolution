"""
Composer DAG: End-to-end CitizenMatch pipeline.

1. Trigger DE's ingestion DAG (GCS → BigQuery)
2. Wait for it to complete
3. Trigger matching pipeline (Cloud Run)

- schedule_interval=None for manual testing
- Switch to "0 7 1 * *" for monthly when ready
"""

from datetime import datetime, timedelta

import requests
from google.oauth2 import id_token
from google.auth.transport.requests import Request
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sensors.external_task import ExternalTaskSensor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLOUD_RUN_URL = "https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app"
DE_DAG_ID     = "gcs_to_bq_unclaimed_property_ingestion"

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
    dag_id="citizenmatch_trigger",
    description="Ingest Kelmar data then run CitizenMatch matching pipeline",
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
        poke_interval=30,            # check every 30 seconds
        execution_timeout=timedelta(minutes=30),
    )

    # Step 2: Run matching pipeline via Cloud Run
    def trigger_matching():
        """POST to Cloud Run using the service account's OIDC token."""
        token = id_token.fetch_id_token(Request(), CLOUD_RUN_URL)
        resp = requests.post(
            CLOUD_RUN_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=3600,
        )
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        if resp.status_code != 200:
            raise RuntimeError(f"Pipeline failed: {resp.status_code} — {resp.text}")
        print("Matching pipeline completed successfully.")

    run_matching = PythonOperator(
        task_id="run_matching_pipeline",
        python_callable=trigger_matching,
        execution_timeout=timedelta(hours=2),
    )

    trigger_ingestion >> run_matching