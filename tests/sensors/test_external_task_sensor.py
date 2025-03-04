#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import logging
import unittest
from datetime import time, timedelta

import pytest

from airflow import exceptions, settings
from airflow.exceptions import AirflowException, AirflowSensorTimeout
from airflow.models import DagBag, DagRun, TaskInstance
from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.dummy import DummyOperator
from airflow.sensors.external_task import ExternalTaskMarker, ExternalTaskSensor
from airflow.sensors.time_sensor import TimeSensor
from airflow.serialization.serialized_objects import SerializedBaseOperator
from airflow.utils.session import provide_session
from airflow.utils.state import DagRunState, State, TaskInstanceState
from airflow.utils.timezone import datetime
from airflow.utils.types import DagRunType
from tests.test_utils.db import clear_db_runs

DEFAULT_DATE = datetime(2015, 1, 1)
TEST_DAG_ID = 'unit_test_dag'
TEST_TASK_ID = 'time_sensor_check'
TEST_TASK_ID_ALTERNATE = 'time_sensor_check_alternate'
DEV_NULL = '/dev/null'


@pytest.fixture(autouse=True)
def clean_db():
    clear_db_runs()


class TestExternalTaskSensor(unittest.TestCase):
    def setUp(self):
        self.dagbag = DagBag(dag_folder=DEV_NULL, include_examples=True)
        self.args = {'owner': 'airflow', 'start_date': DEFAULT_DATE}
        self.dag = DAG(TEST_DAG_ID, default_args=self.args)

    def test_time_sensor(self, task_id=TEST_TASK_ID):
        op = TimeSensor(task_id=task_id, target_time=time(0), dag=self.dag)
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor(self):
        self.test_time_sensor()
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            dag=self.dag,
        )
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_multiple_task_ids(self):
        self.test_time_sensor(task_id=TEST_TASK_ID)
        self.test_time_sensor(task_id=TEST_TASK_ID_ALTERNATE)
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check_task_ids',
            external_dag_id=TEST_DAG_ID,
            external_task_ids=[TEST_TASK_ID, TEST_TASK_ID_ALTERNATE],
            dag=self.dag,
        )
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_catch_overlap_allowed_failed_state(self):
        with pytest.raises(AirflowException):
            ExternalTaskSensor(
                task_id='test_external_task_sensor_check',
                external_dag_id=TEST_DAG_ID,
                external_task_id=TEST_TASK_ID,
                allowed_states=[State.SUCCESS],
                failed_states=[State.SUCCESS],
                dag=self.dag,
            )

    def test_external_task_sensor_wrong_failed_states(self):
        with pytest.raises(ValueError):
            ExternalTaskSensor(
                task_id='test_external_task_sensor_check',
                external_dag_id=TEST_DAG_ID,
                external_task_id=TEST_TASK_ID,
                failed_states=["invalid_state"],
                dag=self.dag,
            )

    def test_external_task_sensor_failed_states(self):
        self.test_time_sensor()
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            failed_states=["failed"],
            dag=self.dag,
        )
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_failed_states_as_success(self):
        self.test_time_sensor()
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            allowed_states=["failed"],
            failed_states=["success"],
            dag=self.dag,
        )
        with self.assertLogs(op.log, level=logging.INFO) as cm:
            with pytest.raises(AirflowException) as ctx:
                op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)
            assert (
                'INFO:airflow.task.operators:Poking for tasks [\'time_sensor_check\']'
                ' in dag unit_test_dag on %s ... ' % DEFAULT_DATE.isoformat() in cm.output
            )
            assert (
                str(ctx.value) == "Some of the external tasks "
                "['time_sensor_check'] in DAG "
                "unit_test_dag failed."
            )

    def test_external_task_sensor_failed_states_as_success_mulitple_task_ids(self):
        self.test_time_sensor(task_id=TEST_TASK_ID)
        self.test_time_sensor(task_id=TEST_TASK_ID_ALTERNATE)
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check_task_ids',
            external_dag_id=TEST_DAG_ID,
            external_task_ids=[TEST_TASK_ID, TEST_TASK_ID_ALTERNATE],
            allowed_states=["failed"],
            failed_states=["success"],
            dag=self.dag,
        )
        with self.assertLogs(op.log, level=logging.INFO) as cm:
            with pytest.raises(AirflowException) as ctx:
                op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)
            assert (
                'INFO:airflow.task.operators:Poking for tasks '
                '[\'time_sensor_check\', \'time_sensor_check_alternate\'] '
                'in dag unit_test_dag on %s ... ' % DEFAULT_DATE.isoformat() in cm.output
            )
            assert (
                str(ctx.value) == "Some of the external tasks "
                "['time_sensor_check', 'time_sensor_check_alternate'] in DAG "
                "unit_test_dag failed."
            )

    def test_external_dag_sensor(self):
        other_dag = DAG('other_dag', default_args=self.args, end_date=DEFAULT_DATE, schedule_interval='@once')
        other_dag.create_dagrun(
            run_id='test', start_date=DEFAULT_DATE, execution_date=DEFAULT_DATE, state=State.SUCCESS
        )
        op = ExternalTaskSensor(
            task_id='test_external_dag_sensor_check',
            external_dag_id='other_dag',
            external_task_id=None,
            dag=self.dag,
        )
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_fn_multiple_execution_dates(self):
        bash_command_code = """
{% set s=logical_date.time().second %}
echo "second is {{ s }}"
if [[ $(( {{ s }} % 60 )) == 1 ]]
    then
        exit 1
fi
exit 0
"""
        dag_external_id = TEST_DAG_ID + '_external'
        dag_external = DAG(dag_external_id, default_args=self.args, schedule_interval=timedelta(seconds=1))
        task_external_with_failure = BashOperator(
            task_id="task_external_with_failure", bash_command=bash_command_code, retries=0, dag=dag_external
        )
        task_external_without_failure = DummyOperator(
            task_id="task_external_without_failure", retries=0, dag=dag_external
        )

        task_external_without_failure.run(
            start_date=DEFAULT_DATE, end_date=DEFAULT_DATE + timedelta(seconds=1), ignore_ti_state=True
        )

        session = settings.Session()
        TI = TaskInstance
        try:
            task_external_with_failure.run(
                start_date=DEFAULT_DATE, end_date=DEFAULT_DATE + timedelta(seconds=1), ignore_ti_state=True
            )
            # The test_with_failure task is excepted to fail
            # once per minute (the run on the first second of
            # each minute).
        except Exception as e:
            failed_tis = (
                session.query(TI)
                .filter(
                    TI.dag_id == dag_external_id,
                    TI.state == State.FAILED,
                    TI.execution_date == DEFAULT_DATE + timedelta(seconds=1),
                )
                .all()
            )
            if len(failed_tis) == 1 and failed_tis[0].task_id == 'task_external_with_failure':
                pass
            else:
                raise e

        dag_id = TEST_DAG_ID
        dag = DAG(dag_id, default_args=self.args, schedule_interval=timedelta(minutes=1))
        task_without_failure = ExternalTaskSensor(
            task_id='task_without_failure',
            external_dag_id=dag_external_id,
            external_task_id='task_external_without_failure',
            execution_date_fn=lambda dt: [dt + timedelta(seconds=i) for i in range(2)],
            allowed_states=['success'],
            retries=0,
            timeout=1,
            poke_interval=1,
            dag=dag,
        )
        task_with_failure = ExternalTaskSensor(
            task_id='task_with_failure',
            external_dag_id=dag_external_id,
            external_task_id='task_external_with_failure',
            execution_date_fn=lambda dt: [dt + timedelta(seconds=i) for i in range(2)],
            allowed_states=['success'],
            retries=0,
            timeout=1,
            poke_interval=1,
            dag=dag,
        )

        task_without_failure.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with pytest.raises(AirflowSensorTimeout):
            task_with_failure.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_delta(self):
        self.test_time_sensor()
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check_delta',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            execution_delta=timedelta(0),
            allowed_states=['success'],
            dag=self.dag,
        )
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_fn(self):
        self.test_time_sensor()
        # check that the execution_fn works
        op1 = ExternalTaskSensor(
            task_id='test_external_task_sensor_check_delta_1',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            execution_date_fn=lambda dt: dt + timedelta(0),
            allowed_states=['success'],
            dag=self.dag,
        )
        op1.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)
        # double check that the execution is being called by failing the test
        op2 = ExternalTaskSensor(
            task_id='test_external_task_sensor_check_delta_2',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            execution_date_fn=lambda dt: dt + timedelta(days=1),
            allowed_states=['success'],
            timeout=1,
            poke_interval=1,
            dag=self.dag,
        )
        with pytest.raises(exceptions.AirflowSensorTimeout):
            op2.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_fn_multiple_args(self):
        """Check this task sensor passes multiple args with full context. If no failure, means clean run."""
        self.test_time_sensor()

        def my_func(dt, context):
            assert context['logical_date'] == dt
            return dt + timedelta(0)

        op1 = ExternalTaskSensor(
            task_id='test_external_task_sensor_multiple_arg_fn',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            execution_date_fn=my_func,
            allowed_states=['success'],
            dag=self.dag,
        )
        op1.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_fn_kwargs(self):
        """Check this task sensor passes multiple args with full context. If no failure, means clean run."""
        self.test_time_sensor()

        def my_func(dt, ds_nodash, tomorrow_ds_nodash):
            assert ds_nodash == dt.strftime("%Y%m%d")
            assert tomorrow_ds_nodash == (dt + timedelta(days=1)).strftime("%Y%m%d")
            return dt + timedelta(0)

        op1 = ExternalTaskSensor(
            task_id='test_external_task_sensor_fn_kwargs',
            external_dag_id=TEST_DAG_ID,
            external_task_id=TEST_TASK_ID,
            execution_date_fn=my_func,
            allowed_states=['success'],
            dag=self.dag,
        )
        op1.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_error_delta_and_fn(self):
        self.test_time_sensor()
        # Test that providing execution_delta and a function raises an error
        with pytest.raises(ValueError):
            ExternalTaskSensor(
                task_id='test_external_task_sensor_check_delta',
                external_dag_id=TEST_DAG_ID,
                external_task_id=TEST_TASK_ID,
                execution_delta=timedelta(0),
                execution_date_fn=lambda dt: dt,
                allowed_states=['success'],
                dag=self.dag,
            )

    def test_external_task_sensor_error_task_id_and_task_ids(self):
        self.test_time_sensor()
        # Test that providing execution_delta and a function raises an error
        with pytest.raises(ValueError):
            ExternalTaskSensor(
                task_id='test_external_task_sensor_task_id_and_task_ids',
                external_dag_id=TEST_DAG_ID,
                external_task_id=TEST_TASK_ID,
                external_task_ids=[TEST_TASK_ID],
                allowed_states=['success'],
                dag=self.dag,
            )

    def test_catch_duplicate_task_ids(self):
        self.test_time_sensor()
        # Test By passing same task_id multiple times
        with pytest.raises(ValueError):
            ExternalTaskSensor(
                task_id='test_external_task_duplicate_task_ids',
                external_dag_id=TEST_DAG_ID,
                external_task_ids=[TEST_TASK_ID, TEST_TASK_ID],
                allowed_states=['success'],
                dag=self.dag,
            )

    def test_catch_invalid_allowed_states(self):
        with pytest.raises(ValueError):
            ExternalTaskSensor(
                task_id='test_external_task_sensor_check_1',
                external_dag_id=TEST_DAG_ID,
                external_task_id=TEST_TASK_ID,
                allowed_states=['invalid_state'],
                dag=self.dag,
            )

        with pytest.raises(ValueError):
            ExternalTaskSensor(
                task_id='test_external_task_sensor_check_2',
                external_dag_id=TEST_DAG_ID,
                external_task_id=None,
                allowed_states=['invalid_state'],
                dag=self.dag,
            )

    def test_external_task_sensor_waits_for_task_check_existence(self):
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check',
            external_dag_id="example_bash_operator",
            external_task_id="non-existing-task",
            check_existence=True,
            dag=self.dag,
        )

        with pytest.raises(AirflowException):
            op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    def test_external_task_sensor_waits_for_dag_check_existence(self):
        op = ExternalTaskSensor(
            task_id='test_external_task_sensor_check',
            external_dag_id="non-existing-dag",
            external_task_id=None,
            check_existence=True,
            dag=self.dag,
        )

        with pytest.raises(AirflowException):
            op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)


def test_external_task_sensor_templated(dag_maker, app):
    with dag_maker():
        ExternalTaskSensor(
            task_id='templated_task',
            external_dag_id='dag_{{ ds }}',
            external_task_id='task_{{ ds }}',
        )

    dagrun = dag_maker.create_dagrun(run_type=DagRunType.SCHEDULED, execution_date=DEFAULT_DATE)
    (instance,) = dagrun.task_instances
    instance.render_templates()

    assert instance.task.external_dag_id == f"dag_{DEFAULT_DATE.date()}"
    assert instance.task.external_task_id == f"task_{DEFAULT_DATE.date()}"

    # Verify that the operator link uses the rendered value of ``external_dag_id``.
    app.config['SERVER_NAME'] = ""
    with app.app_context():
        url = instance.task.get_extra_links(DEFAULT_DATE, "External DAG")

        assert f"tree?dag_id=dag_{DEFAULT_DATE.date()}" in url


class TestExternalTaskMarker(unittest.TestCase):
    def test_serialized_fields(self):
        assert {"recursion_depth"}.issubset(ExternalTaskMarker.get_serialized_fields())

    def test_serialized_external_task_marker(self):
        dag = DAG('test_serialized_external_task_marker', start_date=DEFAULT_DATE)
        task = ExternalTaskMarker(
            task_id="parent_task",
            external_dag_id="external_task_marker_child",
            external_task_id="child_task1",
            dag=dag,
        )

        serialized_op = SerializedBaseOperator.serialize_operator(task)
        deserialized_op = SerializedBaseOperator.deserialize_operator(serialized_op)
        assert deserialized_op.task_type == 'ExternalTaskMarker'
        assert getattr(deserialized_op, 'external_dag_id') == 'external_task_marker_child'
        assert getattr(deserialized_op, 'external_task_id') == 'child_task1'


@pytest.fixture
def dag_bag_ext():
    """
    Create a DagBag with DAGs looking like this. The dotted lines represent external dependencies
    set up using ExternalTaskMarker and ExternalTaskSensor.

    dag_0:   task_a_0 >> task_b_0
                             |
                             |
    dag_1:                   ---> task_a_1 >> task_b_1
                                                  |
                                                  |
    dag_2:                                        ---> task_a_2 >> task_b_2
                                                                       |
                                                                       |
    dag_3:                                                             ---> task_a_3 >> task_b_3
    """
    clear_db_runs()

    dag_bag = DagBag(dag_folder=DEV_NULL, include_examples=False)

    dag_0 = DAG("dag_0", start_date=DEFAULT_DATE, schedule_interval=None)
    task_a_0 = DummyOperator(task_id="task_a_0", dag=dag_0)
    task_b_0 = ExternalTaskMarker(
        task_id="task_b_0", external_dag_id="dag_1", external_task_id="task_a_1", recursion_depth=3, dag=dag_0
    )
    task_a_0 >> task_b_0

    dag_1 = DAG("dag_1", start_date=DEFAULT_DATE, schedule_interval=None)
    task_a_1 = ExternalTaskSensor(
        task_id="task_a_1", external_dag_id=dag_0.dag_id, external_task_id=task_b_0.task_id, dag=dag_1
    )
    task_b_1 = ExternalTaskMarker(
        task_id="task_b_1", external_dag_id="dag_2", external_task_id="task_a_2", recursion_depth=2, dag=dag_1
    )
    task_a_1 >> task_b_1

    dag_2 = DAG("dag_2", start_date=DEFAULT_DATE, schedule_interval=None)
    task_a_2 = ExternalTaskSensor(
        task_id="task_a_2", external_dag_id=dag_1.dag_id, external_task_id=task_b_1.task_id, dag=dag_2
    )
    task_b_2 = ExternalTaskMarker(
        task_id="task_b_2", external_dag_id="dag_3", external_task_id="task_a_3", recursion_depth=1, dag=dag_2
    )
    task_a_2 >> task_b_2

    dag_3 = DAG("dag_3", start_date=DEFAULT_DATE, schedule_interval=None)
    task_a_3 = ExternalTaskSensor(
        task_id="task_a_3", external_dag_id=dag_2.dag_id, external_task_id=task_b_2.task_id, dag=dag_3
    )
    task_b_3 = DummyOperator(task_id="task_b_3", dag=dag_3)
    task_a_3 >> task_b_3

    for dag in [dag_0, dag_1, dag_2, dag_3]:
        dag_bag.bag_dag(dag=dag, root_dag=dag)

    yield dag_bag

    clear_db_runs()


@pytest.fixture
def dag_bag_parent_child():
    """
    Create a DagBag with two DAGs looking like this. task_1 of child_dag_1 on day 1 depends on
    task_0 of parent_dag_0 on day 1. Therefore, when task_0 of parent_dag_0 on day 1 and day 2
    are cleared, parent_dag_0 DagRuns need to be set to running on both days, but child_dag_1
    only needs to be set to running on day 1.

                   day 1   day 2

     parent_dag_0  task_0  task_0
                     |
                     |
                     v
     child_dag_1   task_1  task_1

    """
    clear_db_runs()

    dag_bag = DagBag(dag_folder=DEV_NULL, include_examples=False)

    day_1 = DEFAULT_DATE

    with DAG("parent_dag_0", start_date=day_1, schedule_interval=None) as dag_0:
        task_0 = ExternalTaskMarker(
            task_id="task_0",
            external_dag_id="child_dag_1",
            external_task_id="task_1",
            execution_date=day_1.isoformat(),
            recursion_depth=3,
        )

    with DAG("child_dag_1", start_date=day_1, schedule_interval=None) as dag_1:
        ExternalTaskSensor(
            task_id="task_1",
            external_dag_id=dag_0.dag_id,
            external_task_id=task_0.task_id,
            execution_date_fn=lambda logical_date: day_1 if logical_date == day_1 else [],
            mode='reschedule',
        )

    for dag in [dag_0, dag_1]:
        dag_bag.bag_dag(dag=dag, root_dag=dag)

    yield dag_bag

    clear_db_runs()


@provide_session
def run_tasks(dag_bag, execution_date=DEFAULT_DATE, session=None):
    """
    Run all tasks in the DAGs in the given dag_bag. Return the TaskInstance objects as a dict
    keyed by task_id.
    """
    tis = {}

    for dag in dag_bag.dags.values():
        dagrun = dag.create_dagrun(
            state=State.RUNNING,
            execution_date=execution_date,
            start_date=execution_date,
            run_type=DagRunType.MANUAL,
            session=session,
        )
        # we use sorting by task_id here because for the test DAG structure of ours
        # this is equivalent to topological sort. It would not work in general case
        # but it works for our case because we specifically constructed test DAGS
        # in the way that those two sort methods are equivalent
        tasks = sorted((ti for ti in dagrun.task_instances), key=lambda ti: ti.task_id)
        for ti in tasks:
            ti.refresh_from_task(dag.get_task(ti.task_id))
            tis[ti.task_id] = ti
            ti.run(session=session)
            session.flush()
            session.merge(ti)
            assert_ti_state_equal(ti, State.SUCCESS)

    return tis


def assert_ti_state_equal(task_instance, state):
    """
    Assert state of task_instances equals the given state.
    """
    task_instance.refresh_from_db()
    assert task_instance.state == state


@provide_session
def clear_tasks(
    dag_bag,
    dag,
    task,
    session,
    start_date=DEFAULT_DATE,
    end_date=DEFAULT_DATE,
    dry_run=False,
):
    """
    Clear the task and its downstream tasks recursively for the dag in the given dagbag.
    """
    partial: DAG = dag.partial_subset(task_ids_or_regex=[task.task_id], include_downstream=True)
    return partial.clear(
        start_date=start_date,
        end_date=end_date,
        dag_bag=dag_bag,
        dry_run=dry_run,
        session=session,
    )


def test_external_task_marker_transitive(dag_bag_ext):
    """
    Test clearing tasks across DAGs.
    """
    tis = run_tasks(dag_bag_ext)
    dag_0 = dag_bag_ext.get_dag("dag_0")
    task_a_0 = dag_0.get_task("task_a_0")
    clear_tasks(dag_bag_ext, dag_0, task_a_0)
    ti_a_0 = tis["task_a_0"]
    ti_b_3 = tis["task_b_3"]
    assert_ti_state_equal(ti_a_0, State.NONE)
    assert_ti_state_equal(ti_b_3, State.NONE)


@provide_session
def test_external_task_marker_clear_activate(dag_bag_parent_child, session):
    """
    Test clearing tasks across DAGs and make sure the right DagRuns are activated.
    """
    dag_bag = dag_bag_parent_child
    day_1 = DEFAULT_DATE
    day_2 = DEFAULT_DATE + timedelta(days=1)

    run_tasks(dag_bag, execution_date=day_1)
    run_tasks(dag_bag, execution_date=day_2)

    # Assert that dagruns of all the affected dags are set to SUCCESS before tasks are cleared.
    for dag in dag_bag.dags.values():
        for execution_date in [day_1, day_2]:
            dagrun = dag.get_dagrun(execution_date=execution_date, session=session)
            dagrun.set_state(State.SUCCESS)
    session.flush()

    dag_0 = dag_bag.get_dag("parent_dag_0")
    task_0 = dag_0.get_task("task_0")
    clear_tasks(dag_bag, dag_0, task_0, start_date=day_1, end_date=day_2, session=session)

    # Assert that dagruns of all the affected dags are set to QUEUED after tasks are cleared.
    # Unaffected dagruns should be left as SUCCESS.
    dagrun_0_1 = dag_bag.get_dag('parent_dag_0').get_dagrun(execution_date=day_1, session=session)
    dagrun_0_2 = dag_bag.get_dag('parent_dag_0').get_dagrun(execution_date=day_2, session=session)
    dagrun_1_1 = dag_bag.get_dag('child_dag_1').get_dagrun(execution_date=day_1, session=session)
    dagrun_1_2 = dag_bag.get_dag('child_dag_1').get_dagrun(execution_date=day_2, session=session)

    assert dagrun_0_1.state == State.QUEUED
    assert dagrun_0_2.state == State.QUEUED
    assert dagrun_1_1.state == State.QUEUED
    assert dagrun_1_2.state == State.SUCCESS


def test_external_task_marker_future(dag_bag_ext):
    """
    Test clearing tasks with no end_date. This is the case when users clear tasks with
    Future, Downstream and Recursive selected.
    """
    date_0 = DEFAULT_DATE
    date_1 = DEFAULT_DATE + timedelta(days=1)

    tis_date_0 = run_tasks(dag_bag_ext, execution_date=date_0)
    tis_date_1 = run_tasks(dag_bag_ext, execution_date=date_1)

    dag_0 = dag_bag_ext.get_dag("dag_0")
    task_a_0 = dag_0.get_task("task_a_0")
    # This should clear all tasks on dag_0 to dag_3 on both date_0 and date_1
    clear_tasks(dag_bag_ext, dag_0, task_a_0, end_date=None)

    ti_a_0_date_0 = tis_date_0["task_a_0"]
    ti_b_3_date_0 = tis_date_0["task_b_3"]
    ti_b_3_date_1 = tis_date_1["task_b_3"]
    assert_ti_state_equal(ti_a_0_date_0, State.NONE)
    assert_ti_state_equal(ti_b_3_date_0, State.NONE)
    assert_ti_state_equal(ti_b_3_date_1, State.NONE)


def test_external_task_marker_exception(dag_bag_ext):
    """
    Clearing across multiple DAGs should raise AirflowException if more levels are being cleared
    than allowed by the recursion_depth of the first ExternalTaskMarker being cleared.
    """
    run_tasks(dag_bag_ext)
    dag_0 = dag_bag_ext.get_dag("dag_0")
    task_a_0 = dag_0.get_task("task_a_0")
    task_b_0 = dag_0.get_task("task_b_0")
    task_b_0.recursion_depth = 2
    with pytest.raises(AirflowException, match="Maximum recursion depth 2"):
        clear_tasks(dag_bag_ext, dag_0, task_a_0)


@pytest.fixture
def dag_bag_cyclic():
    """
    Create a DagBag with DAGs having cyclic dependencies set up by ExternalTaskMarker and
    ExternalTaskSensor.

    dag_0:   task_a_0 >> task_b_0
                  ^          |
                  |          |
    dag_1:        |          ---> task_a_1 >> task_b_1
                  |                              ^
                  |                              |
    dag_n:        |                              ---> task_a_n >> task_b_n
                  |                                                   |
                  -----------------------------------------------------
    """

    def _factory(depth: int) -> DagBag:
        dag_bag = DagBag(dag_folder=DEV_NULL, include_examples=False)

        dags = []

        with DAG("dag_0", start_date=DEFAULT_DATE, schedule_interval=None) as dag:
            dags.append(dag)
            task_a_0 = DummyOperator(task_id="task_a_0")
            task_b_0 = ExternalTaskMarker(
                task_id="task_b_0", external_dag_id="dag_1", external_task_id="task_a_1", recursion_depth=3
            )
            task_a_0 >> task_b_0

        for n in range(1, depth):
            with DAG(f"dag_{n}", start_date=DEFAULT_DATE, schedule_interval=None) as dag:
                dags.append(dag)
                task_a = ExternalTaskSensor(
                    task_id=f"task_a_{n}",
                    external_dag_id=f"dag_{n-1}",
                    external_task_id=f"task_b_{n-1}",
                )
                task_b = ExternalTaskMarker(
                    task_id=f"task_b_{n}",
                    external_dag_id=f"dag_{n+1}",
                    external_task_id=f"task_a_{n+1}",
                    recursion_depth=3,
                )
                task_a >> task_b

        # Create the last dag which loops back
        with DAG(f"dag_{depth}", start_date=DEFAULT_DATE, schedule_interval=None) as dag:
            dags.append(dag)
            task_a = ExternalTaskSensor(
                task_id=f"task_a_{depth}",
                external_dag_id=f"dag_{depth-1}",
                external_task_id=f"task_b_{depth-1}",
            )
            task_b = ExternalTaskMarker(
                task_id=f"task_b_{depth}",
                external_dag_id="dag_0",
                external_task_id="task_a_0",
                recursion_depth=2,
            )
            task_a >> task_b

        for dag in dags:
            dag_bag.bag_dag(dag=dag, root_dag=dag)

        return dag_bag

    return _factory


def test_external_task_marker_cyclic_deep(dag_bag_cyclic):
    """
    Tests clearing across multiple DAGs that have cyclic dependencies. AirflowException should be
    raised.
    """
    dag_bag = dag_bag_cyclic(10)
    run_tasks(dag_bag)
    dag_0 = dag_bag.get_dag("dag_0")
    task_a_0 = dag_0.get_task("task_a_0")
    with pytest.raises(AirflowException, match="Maximum recursion depth 3"):
        clear_tasks(dag_bag, dag_0, task_a_0)


def test_external_task_marker_cyclic_shallow(dag_bag_cyclic):
    """
    Tests clearing across multiple DAGs that have cyclic dependencies shallower
    than recursion_depth
    """
    dag_bag = dag_bag_cyclic(2)
    run_tasks(dag_bag)
    dag_0 = dag_bag.get_dag("dag_0")
    task_a_0 = dag_0.get_task("task_a_0")

    tis = clear_tasks(dag_bag, dag_0, task_a_0, dry_run=True)

    assert [
        ("dag_0", "task_a_0"),
        ("dag_0", "task_b_0"),
        ("dag_1", "task_a_1"),
        ("dag_1", "task_b_1"),
        ("dag_2", "task_a_2"),
        ("dag_2", "task_b_2"),
    ] == sorted((ti.dag_id, ti.task_id) for ti in tis)


@pytest.fixture
def dag_bag_multiple():
    """
    Create a DagBag containing two DAGs, linked by multiple ExternalTaskMarker.
    """
    dag_bag = DagBag(dag_folder=DEV_NULL, include_examples=False)
    daily_dag = DAG("daily_dag", start_date=DEFAULT_DATE, schedule_interval="@daily")
    agg_dag = DAG("agg_dag", start_date=DEFAULT_DATE, schedule_interval="@daily")
    dag_bag.bag_dag(dag=daily_dag, root_dag=daily_dag)
    dag_bag.bag_dag(dag=agg_dag, root_dag=agg_dag)

    daily_task = DummyOperator(task_id="daily_tas", dag=daily_dag)

    start = DummyOperator(task_id="start", dag=agg_dag)
    for i in range(25):
        task = ExternalTaskMarker(
            task_id=f"{daily_task.task_id}_{i}",
            external_dag_id=daily_dag.dag_id,
            external_task_id=daily_task.task_id,
            execution_date="{{ macros.ds_add(ds, -1 * %s) }}" % i,
            dag=agg_dag,
        )
        start >> task

    yield dag_bag


@pytest.mark.quarantined
@pytest.mark.backend("postgres", "mysql")
def test_clear_multiple_external_task_marker(dag_bag_multiple):
    """
    Test clearing a dag that has multiple ExternalTaskMarker.

    sqlite3 parser stack size is 100 lexical items by default so this puts a hard limit on
    the level of nesting in the sql. This test is intentionally skipped in sqlite.
    """
    agg_dag = dag_bag_multiple.get_dag("agg_dag")

    for delta in range(len(agg_dag.tasks)):
        execution_date = DEFAULT_DATE + timedelta(days=delta)
        run_tasks(dag_bag_multiple, execution_date=execution_date)

    # There used to be some slowness caused by calling count() inside DAG.clear().
    # That has since been fixed. It should take no more than a few seconds to call
    # dag.clear() here.
    assert agg_dag.clear(start_date=execution_date, end_date=execution_date, dag_bag=dag_bag_multiple) == 51


@pytest.fixture
def dag_bag_head_tail():
    """
    Create a DagBag containing one DAG, with task "head" depending on task "tail" of the
    previous execution_date.

    20200501     20200502                 20200510
    +------+     +------+                 +------+
    | head |    -->head |    -->         -->head |
    |  |   |   / |  |   |   /           / |  |   |
    |  v   |  /  |  v   |  /           /  |  v   |
    | body | /   | body | /     ...   /   | body |
    |  |   |/    |  |   |/           /    |  |   |
    |  v   /     |  v   /           /     |  v   |
    | tail/|     | tail/|          /      | tail |
    +------+     +------+                 +------+
    """
    dag_bag = DagBag(dag_folder=DEV_NULL, include_examples=False)

    with DAG("head_tail", start_date=DEFAULT_DATE, schedule_interval="@daily") as dag:
        head = ExternalTaskSensor(
            task_id='head',
            external_dag_id=dag.dag_id,
            external_task_id="tail",
            execution_delta=timedelta(days=1),
            mode="reschedule",
        )
        body = DummyOperator(task_id="body")
        tail = ExternalTaskMarker(
            task_id="tail",
            external_dag_id=dag.dag_id,
            external_task_id=head.task_id,
            execution_date="{{ macros.ds_add(ds, 1) }}",
        )
        head >> body >> tail

    dag_bag.bag_dag(dag=dag, root_dag=dag)

    return dag_bag


@provide_session
def test_clear_overlapping_external_task_marker(dag_bag_head_tail, session):
    dag: DAG = dag_bag_head_tail.get_dag('head_tail')

    # "Run" 10 times.
    for delta in range(0, 10):
        execution_date = DEFAULT_DATE + timedelta(days=delta)
        dagrun = DagRun(
            dag_id=dag.dag_id,
            state=DagRunState.SUCCESS,
            execution_date=execution_date,
            run_type=DagRunType.MANUAL,
            run_id=f"test_{delta}",
        )
        session.add(dagrun)
        for task in dag.tasks:
            ti = TaskInstance(task=task)
            dagrun.task_instances.append(ti)
            ti.state = TaskInstanceState.SUCCESS
    session.flush()

    # The next two lines are doing the same thing. Clearing the first "head" with "Future"
    # selected is the same as not selecting "Future". They should take similar amount of
    # time too because dag.clear() uses visited_external_tis to keep track of visited ExternalTaskMarker.
    assert dag.clear(start_date=DEFAULT_DATE, dag_bag=dag_bag_head_tail, session=session) == 30
    assert (
        dag.clear(
            start_date=DEFAULT_DATE,
            end_date=execution_date,
            dag_bag=dag_bag_head_tail,
            session=session,
        )
        == 30
    )
