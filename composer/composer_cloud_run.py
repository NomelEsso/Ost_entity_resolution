"""
Composer DAG: Invoke CitizenMatch Cloud Run service, then poll
the audit table until the pipeline reports success.

Schedule: Every Tuesday at 07:00 UTC.
Pattern:  Fire-and-forget POST → sensor polls BigQuery audit row.
"""

from datetime import datetime, timedelta

import requests
from google.oauth2 import id_token
from google.auth.transport.requests import Request
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.sensors.python import PythonSensor
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

# ---------------------------------------------------------------------------
# Constants — update every placeholder before deploying
# ---------------------------------------------------------------------------
FUNCTION_URL = "https://<cloud-run-service>-<hash>-uc.a.run.app"
PROJECT_ID   = "<project-id>"
DATASET      = "<dataset-id>"
TARGET_TABLE = f"{PROJECT_ID}.{DATASET}.<target-table-name>"
AUDIT_TABLE  = f"`{PROJECT_ID}.{DATASET}.<audit-table-name>`"
BQ_LOCATION  = "US"

# ---------------------------------------------------------------------------
# Default args
# ---------------------------------------------------------------------------
default_args = {
    "owner": "Nomel",
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
}

# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------
with DAG(
    dag_id="citizenmatch_cloud_run",
    description="Invoke CitizenMatch Cloud Run pipeline, wait for audit success.",
    start_date=datetime(2025, 7, 7),
    schedule_interval="0 7 * * 2",
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
) as dag:

    def invoke_function():
        """POST to the private Cloud Run endpoint using an OIDC id-token."""
        token = id_token.fetch_id_token(Request(), FUNCTION_URL)
        try:
            requests.post(
                FUNCTION_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=(30, 1),  # 30s connect (cold-start), 1s read
            )
            print("Cloud Run service invoked successfully.")
        except requests.exceptions.ReadTimeout:
            print("Cloud Run invoked (read timeout — expected in fire-and-forget).")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Cloud Run invoke failed: {e}")

    def audit_success(**context):
        """Return True when audit table has a success row for this run."""
        ts = context["ts"]
        sql = f"""
        SELECT COUNT(1) AS c
        FROM {AUDIT_TABLE}
        WHERE TABLE_NAME  = '{TARGET_TABLE}'
          AND ETL_STATUS   = 'ETL Completed Successfully'
          AND LOAD_TIME   >= TIMESTAMP('{ts}')
        """
        hook = BigQueryHook(
            gcp_conn_id="google_cloud_default",
            location=BQ_LOCATION,
            use_legacy_sql=False,
        )
        rows = hook.get_records(sql)
        count = rows[0][0] if rows else 0
        print(f"Audit rows found: {count}")
        return count > 0

    invoke = PythonOperator(
        task_id="invoke_cloud_run",
        python_callable=invoke_function,
        execution_timeout=timedelta(minutes=5),
    )

    wait_for_audit = PythonSensor(
        task_id="wait_for_audit_success",
        python_callable=audit_success,
        poke_interval=120,
        timeout=3600,
        mode="reschedule",
    )

    invoke >> wait_for_audit