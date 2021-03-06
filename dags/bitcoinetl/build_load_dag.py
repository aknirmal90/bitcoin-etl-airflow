from __future__ import print_function

import json
import logging
import os
import time
from datetime import datetime, timedelta

from airflow import DAG
from airflow.contrib.operators.bigquery_operator import BigQueryOperator
from airflow.contrib.sensors.gcs_sensor import GoogleCloudStorageObjectSensor
from airflow.operators.email_operator import EmailOperator
from airflow.operators.python_operator import PythonOperator
from google.cloud.bigquery import TimePartitioning, SchemaField, Client, LoadJobConfig, Table, QueryJobConfig, \
    QueryPriority, CopyJobConfig
from google.cloud.bigquery.job import SourceFormat

logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)


# The following datasets must be created in BigQuery:
# - {chain}_blockchain_raw
# - {chain}_blockchain_temp
# - {chain}_blockchain

def build_load_dag(
        dag_id,
        output_bucket,
        destination_dataset_project_id,
        chain='bitcoin',
        notification_emails=None,
        start_date=datetime(2018, 7, 1),
        schedule_interval='0 0 * * *'):
    dataset_name = '{}_blockchain'.format(chain)
    dataset_name_raw = '{}_blockchain_raw'.format(chain)
    dataset_name_temp = '{}_blockchain_temp'.format(chain)

    environment = {
        'dataset_name': dataset_name,
        'dataset_name_raw': dataset_name_raw,
        'dataset_name_temp': dataset_name_temp,
        'destination_dataset_project_id': destination_dataset_project_id
    }

    default_dag_args = {
        'depends_on_past': False,
        'start_date': start_date,
        'email_on_failure': True,
        'email_on_retry': True,
        'retries': 5,
        'retry_delay': timedelta(minutes=5)
    }

    if notification_emails and len(notification_emails) > 0:
        default_dag_args['email'] = [email.strip() for email in notification_emails.split(',')]

    # Define a DAG (directed acyclic graph) of tasks.
    dag = DAG(
        dag_id,
        catchup=False,
        schedule_interval=schedule_interval,
        default_args=default_dag_args)

    dags_folder = os.environ.get('DAGS_FOLDER', '/home/airflow/gcs/dags')

    def add_load_tasks(task, file_format, allow_quoted_newlines=False):

        wait_sensor = GoogleCloudStorageObjectSensor(
            task_id='wait_latest_{task}'.format(task=task),
            timeout=60 * 60,
            poke_interval=60,
            bucket=output_bucket,
            object='export/{task}/block_date={datestamp}/{task}.{file_format}'.format(
                task=task, datestamp='{{ds}}', file_format=file_format),
            dag=dag
        )

        def load_task():
            client = Client()
            job_config = LoadJobConfig()
            schema_path = os.path.join(dags_folder, 'resources/stages/raw/schemas/{task}.json'.format(task=task))
            job_config.schema = read_bigquery_schema_from_file(schema_path)
            job_config.source_format = SourceFormat.CSV if file_format == 'csv' else SourceFormat.NEWLINE_DELIMITED_JSON
            if file_format == 'csv':
                job_config.skip_leading_rows = 1
            job_config.write_disposition = 'WRITE_TRUNCATE'
            job_config.allow_quoted_newlines = allow_quoted_newlines
            job_config.ignore_unknown_values = True

            export_location_uri = 'gs://{bucket}/export'.format(bucket=output_bucket)
            uri = '{export_location_uri}/{task}/*.{file_format}'.format(
                export_location_uri=export_location_uri, task=task, file_format=file_format)
            table_ref = client.dataset(dataset_name_raw).table(task)
            load_job = client.load_table_from_uri(uri, table_ref, job_config=job_config)
            submit_bigquery_job(load_job, job_config)
            assert load_job.state == 'DONE'

        load_operator = PythonOperator(
            task_id='load_{task}'.format(task=task),
            python_callable=load_task,
            execution_timeout=timedelta(minutes=30),
            dag=dag
        )

        wait_sensor >> load_operator
        return load_operator

    def add_enrich_tasks(task, time_partitioning_field='time', is_view=False, dependencies=None):
        def enrich_task():
            client = Client()

            def enrich_table():
                # Need to use a temporary table because bq query sets field modes to NULLABLE and descriptions to null
                # when writeDisposition is WRITE_TRUNCATE

                # Create a temporary table
                temp_table_name = '{task}_{milliseconds}'.format(task=task, milliseconds=int(round(time.time() * 1000)))
                temp_table_ref = client.dataset(dataset_name_temp).table(temp_table_name)
                table = Table(temp_table_ref)

                description_path = os.path.join(
                    dags_folder, 'resources/stages/enrich/descriptions/{task}.txt'.format(task=task))
                table.description = read_file(description_path)
                if time_partitioning_field is not None:
                    table.time_partitioning = TimePartitioning(field=time_partitioning_field)
                logging.info('Creating table: ' + json.dumps(table.to_api_repr()))

                schema_path = os.path.join(dags_folder, 'resources/stages/enrich/schemas/{task}.json'.format(task=task))
                schema = read_bigquery_schema_from_file(schema_path)
                table.schema = schema

                table = client.create_table(table)
                assert table.table_id == temp_table_name

                # Query from raw to temporary table
                query_job_config = QueryJobConfig()
                # Finishes faster, query limit for concurrent interactive queries is 50
                query_job_config.priority = QueryPriority.INTERACTIVE
                query_job_config.destination = temp_table_ref
                sql_path = os.path.join(dags_folder, 'resources/stages/enrich/sqls/{task}.sql'.format(task=task))
                sql = read_file(sql_path, environment)
                query_job = client.query(sql, location='US', job_config=query_job_config)
                submit_bigquery_job(query_job, query_job_config)
                assert query_job.state == 'DONE'

                # Copy temporary table to destination
                copy_job_config = CopyJobConfig()
                copy_job_config.write_disposition = 'WRITE_TRUNCATE'

                dest_table_name = '{task}'.format(task=task)
                dest_table_ref = client.dataset(dataset_name, project=destination_dataset_project_id).table(dest_table_name)
                copy_job = client.copy_table(temp_table_ref, dest_table_ref, location='US', job_config=copy_job_config)
                submit_bigquery_job(copy_job, copy_job_config)
                assert copy_job.state == 'DONE'

                # Delete temp table
                client.delete_table(temp_table_ref)

            def enrich_view():
                # Create a temporary table
                temp_table_name = '{task}_{milliseconds}'.format(task=task, milliseconds=int(round(time.time() * 1000)))
                temp_table_ref = client.dataset(dataset_name_temp).table(temp_table_name)
                table = Table(temp_table_ref)

                sql_path = os.path.join(dags_folder, 'resources/stages/enrich/sqls/{task}.sql'.format(task=task))
                sql = read_file(sql_path).format(chain=chain)

                table.view_query = sql
                table.view_use_legacy_sql = False

                description_path = os.path.join(
                    dags_folder, 'resources/stages/enrich/descriptions/{task}.txt'.format(task=task))
                table.description = read_file(description_path)
                if time_partitioning_field is not None:
                    table.time_partitioning = TimePartitioning(field=time_partitioning_field)
                logging.info('Creating table: ' + json.dumps(table.to_api_repr()))

                table = client.create_table(table)
                assert table.table_id == temp_table_name

                # Copy temporary table to destination
                copy_job_config = CopyJobConfig()
                copy_job_config.write_disposition = 'WRITE_TRUNCATE'

                dest_table_name = '{task}'.format(task=task)
                dest_table_ref = client.dataset(dataset_name, project=destination_dataset_project_id).table(dest_table_name)
                copy_job = client.copy_table(temp_table_ref, dest_table_ref, location='US', job_config=copy_job_config)
                submit_bigquery_job(copy_job, copy_job_config)
                assert copy_job.state == 'DONE'

                # Delete temp table
                client.delete_table(temp_table_ref)

            if is_view:
                enrich_view()
            else:
                enrich_table()

        enrich_operator = PythonOperator(
            task_id='enrich_{task}'.format(task=task),
            python_callable=enrich_task,
            execution_timeout=timedelta(minutes=60),
            dag=dag
        )

        if dependencies is not None and len(dependencies) > 0:
            for dependency in dependencies:
                dependency >> enrich_operator
        return enrich_operator

    def add_verify_tasks(task, dependencies=None):
        # The queries in verify/sqls will fail when the condition is not met
        # Have to use this trick since the Python 2 version of BigQueryCheckOperator doesn't support standard SQL
        # and legacy SQL can't be used to query partitioned tables.
        sql_path = os.path.join(dags_folder, 'resources/stages/verify/sqls/{task}.sql'.format(task=task))
        sql = read_file(sql_path, environment)
        verify_task = BigQueryOperator(
            task_id='verify_{task}'.format(task=task),
            sql=sql,
            use_legacy_sql=False,
            dag=dag)
        if dependencies is not None and len(dependencies) > 0:
            for dependency in dependencies:
                dependency >> verify_task
        return verify_task

    load_blocks_task = add_load_tasks('blocks', 'json')
    load_transactions_task = add_load_tasks('transactions', 'json')

    enrich_blocks_task = add_enrich_tasks(
        'blocks', time_partitioning_field='timestamp_month', dependencies=[load_blocks_task])
    enrich_transactions_task = add_enrich_tasks(
        'transactions', time_partitioning_field='block_timestamp_month', dependencies=[load_transactions_task])
    enrich_inputs_task = add_enrich_tasks('inputs', is_view=True, time_partitioning_field=None,
            dependencies=[enrich_transactions_task])
    enrich_outputs_task = add_enrich_tasks('outputs', is_view=True, time_partitioning_field=None,
            dependencies=[enrich_transactions_task])

    verify_blocks_count_task = add_verify_tasks('blocks_count', [enrich_blocks_task])
    verify_blocks_have_latest_task = add_verify_tasks('blocks_have_latest', [enrich_blocks_task])
    verify_transactions_count_task = add_verify_tasks('transactions_count',
                                                      [enrich_blocks_task, enrich_transactions_task])
    verify_transactions_have_latest_task = add_verify_tasks('transactions_have_latest', [enrich_transactions_task])

    # Fees in Dogecoin can be negative
    if chain != 'dogecoin':
        verify_transactions_fees_task = add_verify_tasks('transactions_fees', [enrich_transactions_task])
    verify_coinbase_transactions_count_task = add_verify_tasks('coinbase_transactions_count',
                                                               [enrich_blocks_task, enrich_transactions_task])
    verify_transaction_inputs_count_task = add_verify_tasks('transaction_inputs_count',
                                                            [enrich_transactions_task])
    verify_transaction_outputs_count_task = add_verify_tasks('transaction_outputs_count',
                                                             [enrich_transactions_task])

    # Zcash can have empty inputs and outputs if transaction has join-splits
    if chain != 'zcash':
        verify_transaction_inputs_count_empty_task = add_verify_tasks('transaction_inputs_count_empty',
                                                                [enrich_transactions_task])
        verify_transaction_outputs_count_empty_task = add_verify_tasks('transaction_outputs_count_empty',
                                                                 [enrich_transactions_task])

    # if notification_emails and len(notification_emails) > 0:
    #     send_email_task = EmailOperator(
    #         task_id='send_email',
    #         to=[email.strip() for email in notification_emails.split(',')],
    #         subject='Bitcoin ETL Airflow Load DAG Succeeded',
    #         html_content='Bitcoin ETL Airflow Load DAG Succeeded',
    #         dag=dag
    #     )
    # verify_blocks_count_task >> send_email_task
    # verify_blocks_have_latest_task >> send_email_task
    # verify_transactions_count_task >> send_email_task
    # verify_transactions_have_latest_task >> send_email_task

    return dag


def submit_bigquery_job(job, configuration):
    try:
        logging.info('Creating a job: ' + json.dumps(configuration.to_api_repr()))
        result = job.result()
        logging.info(result)
        assert job.errors is None or len(job.errors) == 0
        return result
    except Exception:
        logging.info(job.errors)
        raise


def read_bigquery_schema_from_file(filepath):
    file_content = read_file(filepath)
    json_content = json.loads(file_content)
    return read_bigquery_schema_from_json_recursive(json_content)


def read_file(filepath, environment=None):
    if environment is None:
        environment = {}

    with open(filepath) as file_handle:
        content = file_handle.read()
        for key, value in environment.items():
            # each bracket should be doubled to be escaped
            # we need two escaped and one unescaped
            content = content.replace('{{{{{key}}}}}'.format(key=key), value)
        return content


def read_bigquery_schema_from_json_recursive(json_schema):
    """
    CAUTION: Recursive function
    This method can generate BQ schemas for nested records
    """
    result = []
    for field in json_schema:
        if field.get('type').lower() == 'record' and field.get('fields'):
            schema = SchemaField(
                name=field.get('name'),
                field_type=field.get('type', 'STRING'),
                mode=field.get('mode', 'NULLABLE'),
                description=field.get('description'),
                fields=read_bigquery_schema_from_json_recursive(field.get('fields'))
            )
        else:
            schema = SchemaField(
                name=field.get('name'),
                field_type=field.get('type', 'STRING'),
                mode=field.get('mode', 'NULLABLE'),
                description=field.get('description')
            )
        result.append(schema)
    return result
