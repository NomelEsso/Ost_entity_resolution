"""
Composer DAG: End-to-end CitizenMatch pipeline (fully automated).

Flow:
  0. Short-circuit: proceed only if a NEW, unprocessed Kelmar file exists.
     On an empty day this cleanly SKIPS the run (green, with a logged reason)
     instead of triggering ingestion and tokenizing for nothing.
  1. Trigger DE's ingestion DAG (GCS -> BigQuery)
  2. Call Cloud Run with full timeout (keep connection alive)
  3. If response lost, check BigQuery and retry
  4. Trigger audit pipeline when complete

IMPORTANT: Cloud Run terminates containers when no active request exists.
We MUST keep the HTTP connection alive for the full duration. Fire-and-forget
does NOT work with Cloud Run.

Schedule: 13:00 UTC (8 AM CDT) on days 1-5 of each month. The DE's ingestion
runs its own window at 07:00 UTC; this runs after, so any load has finished.
The short-circuit dedups against the DE's processed-files audit table, so
running daily across days 1-5 catches a late Kelmar drop without reprocessing.
"""

import time
from datetime import datetime, timedelta

import requests
from google.oauth2 import id_token
from google.auth.transport.requests import Request
from airflow import DAG
from airflow.operators.python import PythonOperator, ShortCircuitOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook
from airflow.models import Variable

# ---------------------------------------------------------------------------
# Environment config -- driven by Airflow Variables so the same DAG runs
# against dev/test/prod by changing Variables, not code. Defaults are dev (-np).
# ---------------------------------------------------------------------------
PROJECT_ID     = Variable.get("citizenmatch_project_id",      default_var="aw-ost-property-np")
OUTPUT_DATASET = Variable.get("citizenmatch_output_dataset",  default_var="citizen_match")
MPI_DATASET    = Variable.get("citizenmatch_mpi_dataset",     default_var="citizen_mpi_result")
BQ_LOCATION    = Variable.get("citizenmatch_bq_location",     default_var="us-central1")
CLOUD_RUN_URL  = Variable.get("citizenmatch_cloud_run_url",   default_var="https://citizenmatch-pipeline-s3dq7cyrzq-uc.a.run.app")

# DE-owned DAGs -- confirm the test names with the data engineer before the run
DE_DAG_ID      = Variable.get("citizenmatch_ingestion_dag_id", default_var="gcs_to_bq_unclaimed_property_ingestion")
AUDIT_DAG_ID   = Variable.get("citizenmatch_audit_dag_id",     default_var="kelmar_outbound")

# Derived table paths the status poller watches
SOK_TOKENIZED_TBL = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_staging_dataset_tokenized_v2"
CHECKPOINT_TBL    = f"{PROJECT_ID}.{OUTPUT_DATASET}.sok_tokenization_checkpoint"
OUTPUT_TBL        = f"{PROJECT_ID}.{MPI_DATASET}.OK_OST_OMES_OUTBOUND_DataMatch"

MAX_ITERATIONS = 10

# ---------------------------------------------------------------------------
# Default args -- 3 retries with 2 min delay
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
    description="Ingest Kelmar -> tokenize SOK -> match -> deliver (fully automated)",
    start_date=datetime(2025, 1, 1),
    # 13:00 UTC (8 AM CDT) on days 1-5 each month, after the DE ingestion window.
    schedule_interval="0 13 1-5 * *",
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["citizenmatch", "entity-resolution"],
) as dag:

    def check_for_new_file(**context):
        """Return True only if a NEW, unprocessed Kelmar file exists.

        Reads the SAME Airflow Variables and processed-files audit table the
        DE's ingestion DAG uses, so this check cannot drift from hers. When it
        returns False, ShortCircuitOperator marks all downstream tasks SKIPPED
        (green), and the printed reason explains why the day was skipped.

        Why a pre-check instead of trusting the ingestion trigger: the DE's DAG
        raises AirflowSkipException on an empty day, but a run whose tasks all
        skip still reports SUCCESS at the DAG-run level. So triggering first and
        reading the run state would falsely look like "loaded" and proceed to
        tokenize. Checking for the file up front avoids that.
        """
        import re
        from datetime import datetime as _dt, date as _date
        from google.cloud import storage, bigquery

        project        = Variable.get("var_project_id",                   default_var=PROJECT_ID)
        bucket         = Variable.get("var_gcs_bucket",                    default_var="kelmar_inbound_files_t")
        prefix         = Variable.get("var_unclaimed_property_gcs_prefix", default_var="kelmar_inbound_files")
        audit_dataset  = Variable.get("var_bq_audit_dataset",             default_var="auditing")
        processed_tbl  = Variable.get("var_bq_processed_files_table",     default_var="processed_files")
        enforce_future = Variable.get("var_enforce_not_future",   default_var="true").strip().lower() in ("1", "true", "yes")
        allow_rerun    = Variable.get("var_allow_rerun_same_file", default_var="false").strip().lower() in ("1", "true", "yes")

        name_re = re.compile(r"^OK_OST_OMES_DataMatch_(\d{8})\.csv$")

        # Files the DE already loaded successfully (skip unless rerun allowed).
        processed = set()
        if not allow_rerun:
            try:
                bq = bigquery.Client(project=project)
                q = f"""
                    SELECT DISTINCT source_file_name
                    FROM `{project}.{audit_dataset}.{processed_tbl}`
                    WHERE status = 'SUCCESS'
                """
                processed = {r["source_file_name"] for r in bq.query(q).result()}
            except Exception as e:
                print(f"Could not read processed-files audit table ({e}); "
                      f"assuming none processed yet.")

        gcs = storage.Client(project=project)
        candidates = []
        for blob in gcs.list_blobs(bucket, prefix=prefix):
            base = blob.name.split("/")[-1]
            m = name_re.match(base)
            if not m:
                continue
            try:
                file_date = _dt.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if enforce_future and file_date > _date.today():
                print(f"Ignoring future-dated file: {blob.name}")
                continue
            if (not allow_rerun) and (blob.name in processed or base in processed):
                continue
            candidates.append((file_date, blob.name))

        if not candidates:
            print(
                f"SKIP: no new Kelmar file to process in "
                f"gs://{bucket}/{prefix} as of {_date.today().isoformat()} UTC. "
                f"Already-processed files are excluded. Nothing to do this run."
            )
            return False

        candidates.sort(reverse=True)
        newest = candidates[0][1]
        print(f"PROCEED: new Kelmar file found -> gs://{bucket}/{newest}")
        return True

    check_new_file = ShortCircuitOperator(
        task_id="check_for_new_file",
        python_callable=check_for_new_file,
        execution_timeout=timedelta(minutes=5),
    )

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
            location=BQ_LOCATION,
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
            tokenized  = _bq_count(f"SELECT COUNT(*) FROM `{SOK_TOKENIZED_TBL}`")
            checkpoint = _bq_count(f"SELECT COUNT(*) FROM `{CHECKPOINT_TBL}`")
            output     = _bq_count(f"SELECT COUNT(*) FROM `{OUTPUT_TBL}`")
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

            # Call Cloud Run -- keep connection alive
            print("Calling Cloud Run (keeping connection alive)...")
            token = id_token.fetch_id_token(Request(), CLOUD_RUN_URL)

            try:
                resp = requests.post(
                    CLOUD_RUN_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=3600,  # 1 hour -- must keep alive
                )
                print(f"Response: {resp.status_code} -- {resp.text}")

                if resp.status_code == 200:
                    # Cloud Run finished -- check what happened
                    time.sleep(10)
                    tokenized, checkpoint, output = _get_status()

                    if output > 0 and checkpoint == 0:
                        print(f"Pipeline complete! Output: {output} rows")
                        return

                    if checkpoint > 0:
                        print("More tokenization needed -- continuing...")
                        continue

                    if checkpoint == 0 and output == 0:
                        print("Tokenization done, matching may need another trigger...")
                        continue
                else:
                    print(f"Non-200 response -- checking BigQuery...")
                    time.sleep(30)
                    tokenized, checkpoint, output = _get_status()
                    if output > 0:
                        print(f"Pipeline complete despite error! Output: {output}")
                        return
                    # Will retry in next iteration

            except requests.exceptions.ReadTimeout:
                # Connection dropped but Cloud Run may have finished
                print("ReadTimeout -- checking BigQuery for progress...")
                time.sleep(30)
                new_tokenized, checkpoint, output = _get_status()

                print(f"After timeout: tokenized={new_tokenized}, "
                      f"checkpoint={'yes' if checkpoint else 'no'}, "
                      f"output={output}")

                if output > 0 and checkpoint == 0:
                    print(f"Pipeline complete! Output: {output} rows")
                    return

                if new_tokenized > tokenized:
                    print("Progress detected -- continuing...")
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
        trigger_dag_id=AUDIT_DAG_ID,
        wait_for_completion=False,
        execution_timeout=timedelta(minutes=5),
    )

    check_new_file >> trigger_ingestion >> run_pipeline >> trigger_audit