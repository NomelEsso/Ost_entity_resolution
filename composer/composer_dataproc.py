"""
Composer DAG: Ephemeral Dataproc cluster for PySpark ETL.

Lifecycle:
  1. Create cluster with Oracle JDBC + Python dependencies.
  2. Submit PySpark job from GCS.
  3. Delete cluster (runs even on failure via ALL_DONE trigger).

Schedule: Daily at 04:00 America/Chicago.
"""

from airflow import models
from airflow.providers.google.cloud.operators.dataproc import (
    DataprocCreateClusterOperator,
    DataprocDeleteClusterOperator,
    DataprocSubmitJobOperator,
)
from airflow.utils.trigger_rule import TriggerRule
import pendulum

# ---------------------------------------------------------------------------
# Constants — update every placeholder before deploying
# ---------------------------------------------------------------------------
PROJECT_ID              = "<project-id>"
REGION                  = "us-central1"
CLUSTER_NAME            = "<cluster-name>"
SUBNETWORK_URI          = "<subnetwork-uri>"
CLUSTER_SERVICE_ACCOUNT = "<service-account>@<project-id>.iam.gserviceaccount.com"

# GCS paths
PYSPARK_URI     = "gs://<code-bucket>/<pyspark-entrypoint>.py"
INIT_ACTION_URI = "gs://<code-bucket>/<init-script>.sh"
OJDBC_JAR_URI   = "gs://<code-bucket>/ojdbc8.jar"

# Schedule
LOCAL_TZ   = pendulum.timezone("America/Chicago")
START_DATE = pendulum.datetime(2025, 1, 1, 0, 0, tz=LOCAL_TZ)

# Pip packages
PIP_PACKAGES = ",".join([
    "pandas==2.2.2",
    "sqlalchemy==2.0.32",
    "cx_Oracle==8.3.0",
    "google-cloud-secret-manager==2.20.0",
    "google-cloud-bigquery==3.25.0",
    "google-cloud-storage==2.18.1",
    "fastavro==1.9.4",
    "pytz==2024.1",
    "pyarrow==10.0.1",
])

# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------
with models.DAG(
    dag_id="dataproc_pyspark_etl",
    description="Create Dataproc cluster, run PySpark job, then delete cluster.",
    schedule_interval="0 4 * * *",
    start_date=START_DATE,
    catchup=False,
    tags=["dataproc", "composer"],
) as dag:

    create_cluster = DataprocCreateClusterOperator(
        task_id="create_cluster",
        project_id=PROJECT_ID,
        region=REGION,
        cluster_name=CLUSTER_NAME,
        cluster_config={
            "gce_cluster_config": {
                "subnetwork_uri": SUBNETWORK_URI,
                "service_account": CLUSTER_SERVICE_ACCOUNT,
                "service_account_scopes": [
                    "https://www.googleapis.com/auth/cloud-platform",
                ],
            },
            "master_config": {
                "num_instances": 1,
                "machine_type_uri": "n2-standard-8",
            },
            "worker_config": {
                "num_instances": 2,
            },
            "software_config": {
                "image_version": "2.1-rocky8",
                "optional_components": ["JUPYTER"],
                "properties": {
                    "dataproc:dataproc.conscrypt.provider.enable": "false",
                    "spark:spark.jars": OJDBC_JAR_URI,
                    "dataproc:pip.packages": PIP_PACKAGES,
                },
            },
            "endpoint_config": {
                "enable_http_port_access": True,
            },
            "initialization_actions": [
                {"executable_file": INIT_ACTION_URI},
            ],
            "lifecycle_config": {
                "idle_delete_ttl": "9200s",
            },
        },
    )

    submit_pyspark_job = DataprocSubmitJobOperator(
        task_id="submit_pyspark_job",
        region=REGION,
        project_id=PROJECT_ID,
        retries=0,
        job={
            "placement": {"cluster_name": CLUSTER_NAME},
            "pyspark_job": {"main_python_file_uri": PYSPARK_URI},
        },
    )

    delete_cluster = DataprocDeleteClusterOperator(
        task_id="delete_cluster",
        project_id=PROJECT_ID,
        region=REGION,
        cluster_name=CLUSTER_NAME,
        trigger_rule=TriggerRule.ALL_DONE,
    )

    create_cluster >> submit_pyspark_job >> delete_cluster