from airflow import DAG
from airflow.operators.postgres_operator import PostgresOperator as PGO
from airflow.sensors.external_task_sensor import ExternalTaskSensor as ETS

from ewah.ewah_utils.airflow_utils import etl_schema_tasks
from ewah.constants import EWAHConstants as EC

from datetime import datetime, timedelta
from copy import deepcopy

class ExtendedETS(ETS):
    """Extend ETS functionality to support the interplay of backfill and
    incremental DAGs."""

    def __init__(self,
        backfill_dag_id=None,
        backfill_execution_delta=None,
        backfill_execution_date_fn=None,
        backfill_external_task_id=None,
    *args, **kwargs):
        self.backfill_dag_id = backfill_dag_id
        self.backfill_execution_delta = backfill_execution_delta
        self.backfill_execution_date_fn = backfill_execution_date_fn
        self.backfill_external_task_id = backfill_external_task_id
        super().__init__(*args, **kwargs)


    def execute(self, context):

        if context['dag'].start_date == context['execution_date']:
            if self.backfill_dag_id:
                # Check if the latest backfill ran! --> then run normally
                self.execution_delta = self.backfill_execution_delta or self.execution_delta
                self.execution_date_fn = self.backfill_execution_date_fn or self.execution_date_fn
                self.external_task_id = self.backfill_external_task_id or self.external_task_id
                self.external_dag_id = self.backfill_dag_id
                self.log.info('First instance, looking for previous backfill!')
                super().execute(context)
            else:
                # return true if this is the first instance and no backfills!
                self.log.info('This is the first execution of the DAG. Thus, ' + \
                'the sensor automatically succeeds.')
        else:
            super().execute(context)

def dag_factory_incremental_loading(
        dag_base_name,
        dwh_engine,
        dwh_conn_id,
        airflow_conn_id,
        start_date,
        etl_operator,
        operator_config,
        target_schema_name,
        target_schema_suffix='_next',
        target_database_name=None,
        default_args=None,
        schedule_interval_backfill=timedelta(days=1),
        schedule_interval_future=timedelta(hours=1),
        switch_date=None,
    ):

    if not hasattr(etl_operator, '_IS_INCREMENTAL'):
        raise Exception('Invalid operator supplied!')
    if not etl_operator._IS_INCREMENTAL:
        raise Exception('Operator does not support incremental loading!')
    if not type(schedule_interval_future) == timedelta:
        raise Exception('Schedule intervals must be datetime.timedelta!')
    if not type(schedule_interval_backfill) == timedelta:
        raise Exception('Schedule intervals must be datetime.timedelta!')
    if schedule_interval_backfill < timedelta(days=1):
        raise Exception('Backfill schedule interval cannot be below 1 day!')
    if schedule_interval_backfill < schedule_interval_future:
        raise Exception('Backfill schedule interval must be larger than' \
            + ' regular schedule interval!')
    if not operator_config.get('tables'):
        raise Exception('Requires a "tables" dictionary in operator_config!')

    if not switch_date:
        current_time = datetime.now() - timedelta(hours=12) # don't switch immediately
        switch_date = int((current_time-start_date)/schedule_interval_backfill)
        switch_date *= schedule_interval_backfill
        switch_date += start_date

    backfill_timedelta = switch_date - start_date
    backfill_tasks_count = backfill_timedelta / schedule_interval_backfill
    # The schedule interval of the backfill must be an exact integer multiple
    # of the time period between start date and switch date!
    if not (backfill_tasks_count == round(backfill_tasks_count, 0)):
        raise Exception('The schedule interval of the backfill must be an ' \
            + 'exact integer multiple of the time period between start date '\
            + 'and switch date!')

    dags = (
        DAG(
            dag_base_name+'_Incremental',
            start_date=switch_date,
            schedule_interval=schedule_interval_future,
            catchup=True,
            max_active_runs=1,
            default_args=default_args,
        ),
        DAG(
            dag_base_name+'_Incremental_Backfill',
            start_date=start_date,
            end_date=switch_date,
            schedule_interval=schedule_interval_backfill,
            catchup=True,
            max_active_runs=1,
            default_args=default_args,
        ),
        DAG(
            dag_base_name+'_Incremental_Reset',
            start_date=start_date,
            schedule_interval=None,
            catchup=False,
            max_active_runs=1,
            default_args=default_args,
        ),
    )

    # Create reset DAG
    reset_sql = """
        DELETE FROM dag_run
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM job
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM task_fail
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM task_instance
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM task_reschedule
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM xcom
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM dag_stats
        WHERE dag_id LIKE %(dag_name)s;
        DELETE FROM dag_run
        WHERE dag_id LIKE %(dag_name_backfill)s;
        DELETE FROM job
        WHERE dag_id LIKE %(dag_name_backfill)s;
        DELETE FROM task_fail
        WHERE dag_id LIKE %(dag_name_backfill)s;
        DELETE FROM task_instance
        WHERE dag_id LIKE %(dag_name_backfill)s;
        DELETE FROM task_reschedule
        WHERE dag_id LIKE %(dag_name_backfill)s;
        DELETE FROM xcom
        WHERE dag_id LIKE %(dag_name_backfill)s;
        DELETE FROM dag_stats
        WHERE dag_id LIKE %(dag_name_backfill)s;
    """
    reset_task = PGO(
        sql=reset_sql,
        postgres_conn_id=airflow_conn_id,
        parameters={
            'dag_name':dag_base_name+'_Incremental',
            'dag_name_backfill':dag_base_name+'_Incremental_Backfill',
        },
        task_id='reset_by_deleting_all_task_instances',
        dag=dags[2],
    )
    drop_sql = f'DROP SCHEMA IF EXISTS "{target_schema_name}" CASCADE;'
    drop_sql += '\nDROP SCHEMA IF EXISTS "{schema}" CASCADE;'.format(**{
        'schema': target_schema_name + target_schema_suffix,
    })
    if dwh_engine == EC.DWH_ENGINE_POSTGRES:
        drop_task = PGO(
            sql=drop_sql,
            postgres_conn_id=dwh_conn_id,
            task_id='delete_previous_schema_if_exists',
            dag=dags[2],
        )
    elif dwh_engine == EC.DWH_ENGINE_SNOWFLAKE:
        drop_task = SnowflakeOperator(
            sql=drop_sql,
            snowflake_conn_id=dwh_conn_id,
            database=target_database_name,
            task_id='delete_previous_schema_if_exists',
            dag=dags[2],
        )
    else:
        raise ValueError('DWH not implemented for this task!')
    reset_task >> drop_task

    # Incremental DAG schema tasks
    kickoff, final = etl_schema_tasks(
        dag=dags[0],
        dwh_engine=dwh_engine,
        copy_schema=True,
        target_schema_name=target_schema_name,
        target_schema_suffix=target_schema_suffix,
        dwh_conn_id=dwh_conn_id,
    )

    # Backfill DAG schema tasks
    kickoff_backfill, final_backfill = etl_schema_tasks(
        dag=dags[1],
        dwh_engine=dwh_engine,
        copy_schema=True,
        target_schema_name=target_schema_name,
        target_schema_suffix=target_schema_suffix,
        dwh_conn_id=dwh_conn_id,
    )

    # Make sure incremental loading stops if there is an error!
    ets = (
        ExtendedETS(
            task_id='sense_previous_instance',
            allowed_states=['success', 'skipped'],
            external_dag_id=dags[0]._dag_id,
            external_task_id=final.task_id,
            execution_delta=schedule_interval_future,
            backfill_dag_id=dags[1]._dag_id,
            backfill_external_task_id=final_backfill.task_id,
            backfill_execution_delta=schedule_interval_backfill,
            dag=dags[0],
        ),
        ExtendedETS(
            task_id='sense_previous_instance',
            allowed_states=['success', 'skipped'],
            external_dag_id=dags[1]._dag_id,
            external_task_id=final_backfill.task_id,
            execution_delta=schedule_interval_backfill,
            dag=dags[1],
        )
    )
    ets[0] >> kickoff
    ets[1] >> kickoff_backfill

    # add table creation tasks
    count_backfill_tasks = 0
    for table in operator_config['tables'].keys():
        arg_dict = {
            'task_id': 'extract_load_' + table,
            'dwh_engine': dwh_engine,
            'dwh_conn_id': dwh_conn_id,
            'target_table_name': table,
            'target_schema_name': target_schema_name,
            'target_schema_suffix': target_schema_suffix,
            'target_database_name': target_database_name,
            'drop_and_replace': False,
            # columns_definition
            # update_on_columns
            # primary_key_column_name
        }
        arg_dict.update(operator_config.get('general_config', {}))
        arg_dict_backfill = deepcopy(arg_dict)
        arg_dict.update(operator_config.get('incremental_config', {}))
        arg_dict_backfill.update(operator_config.get('backfill_config', {}))
        arg_dict.update(operator_config['tables'][table] or {})
        arg_dict_backfill.update(operator_config['tables'][table] or {})

        task = etl_operator(dag=dags[0], **arg_dict)
        kickoff >> task >> final

        if not arg_dict.get('skip_backfill', False):
            task = etl_operator(dag=dags[1], **arg_dict_backfill)
            kickoff_backfill >> task >> final_backfill
            count_backfill_tasks += 1

    if count_backfill_tasks == 0:
        kickoff_backfill >> final_backfill

    return dags
