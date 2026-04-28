"""
Composer DAG: Trigger CitizenMatch Cloud Run pipeline.

- Manual trigger for testing (schedule_interval=None)
- Switch to monthly when ready: schedule_interval="0 7 1 * *"
"""

from datetime import datetime, timedelta

import requests
from google.oauth2 import id_token
from google.auth.transport.requests import Request
from airflow import DAG
from airflow.operators.python import PythonOperator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLOUD_RUN_URL = "https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app"

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
    description="Trigger CitizenMatch Cloud Run pipeline",
    start_date=datetime(2025, 1, 1),
    schedule_interval=None,          # manual trigger only — change to "0 7 1 * *" for monthly
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["citizenmatch", "entity-resolution"],
) as dag:

    def trigger_pipeline():
        """POST to Cloud Run using the service account's OIDC token."""
        token = id_token.fetch_id_token(Request(), CLOUD_RUN_URL)
        resp = requests.post(
            CLOUD_RUN_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=3600,  # wait up to 1 hour for pipeline to finish
        )
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        if resp.status_code != 200:
            raise RuntimeError(f"Pipeline failed: {resp.status_code} — {resp.text}")
        print("Pipeline completed successfully.")

    trigger = PythonOperator(
        task_id="trigger_citizenmatch_pipeline",
        python_callable=trigger_pipeline,
        execution_timeout=timedelta(hours=2),
    )