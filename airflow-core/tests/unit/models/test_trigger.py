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
from __future__ import annotations

import datetime
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pendulum
import pytest
import pytz
from cryptography.fernet import Fernet

from airflow._shared.timezones import timezone
from airflow.jobs.job import Job
from airflow.jobs.triggerer_job_runner import TriggererJobRunner
from airflow.models import Deadline, TaskInstance, Trigger
from airflow.models.asset import AssetEvent, AssetModel, asset_trigger_association_table
from airflow.models.xcom import XComModel
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.serialization.serialized_objects import BaseSerialization
from airflow.triggers.base import (
    BaseTrigger,
    TaskFailedEvent,
    TaskSkippedEvent,
    TaskSuccessEvent,
    TriggerEvent,
)
from airflow.utils.session import create_session
from airflow.utils.state import State

from tests_common.test_utils.config import conf_vars
from unit.models import DEFAULT_DATE

pytestmark = pytest.mark.db_test


@pytest.fixture
def session():
    """Fixture that provides a SQLAlchemy session"""
    with create_session() as session:
        yield session


@pytest.fixture(autouse=True)
def clear_db(session):
    session.query(TaskInstance).delete()
    session.query(asset_trigger_association_table).delete()
    session.query(Deadline).delete()
    session.query(Trigger).delete()
    session.query(AssetModel).delete()
    session.query(AssetEvent).delete()
    session.query(Job).delete()
    yield session
    session.query(TaskInstance).delete()
    session.query(asset_trigger_association_table).delete()
    session.query(Deadline).delete()
    session.query(Trigger).delete()
    session.query(AssetModel).delete()
    session.query(AssetEvent).delete()
    session.query(Job).delete()
    session.commit()


def test_fetch_trigger_ids_with_non_task_associations(session):
    # Create triggers
    asset_trigger = Trigger(classpath="airflow.triggers.testing.SuccessTrigger1", kwargs={})
    deadline_trigger = Trigger(classpath="airflow.triggers.testing.SuccessTrigger2", kwargs={})
    other_trigger = Trigger(classpath="airflow.triggers.testing.SuccessTrigger3", kwargs={})
    session.bulk_save_objects((asset_trigger, deadline_trigger, other_trigger))

    # Create asset association
    asset = AssetModel("test")
    asset.triggers.append(asset_trigger)
    session.add(asset)

    # Create deadline association
    deadline = Deadline(deadline_time=DEFAULT_DATE, callback="classpath.log.error")
    deadline.trigger = deadline_trigger
    session.add(deadline)

    session.commit()
    results = Trigger.fetch_trigger_ids_with_non_task_associations()
    assert results == {asset_trigger.id, deadline_trigger.id}


def test_clean_unused(session, create_task_instance):
    """
    Tests that unused triggers (those with no task instances referencing them)
    are cleaned out automatically.
    """
    # Create triggers
    trigger1 = Trigger(classpath="airflow.triggers.testing.SuccessTrigger1", kwargs={})
    trigger2 = Trigger(classpath="airflow.triggers.testing.SuccessTrigger2", kwargs={})
    trigger3 = Trigger(classpath="airflow.triggers.testing.SuccessTrigger3", kwargs={})
    trigger4 = Trigger(classpath="airflow.triggers.testing.SuccessTrigger4", kwargs={})
    trigger5 = Trigger(classpath="airflow.triggers.testing.SuccessTrigger5", kwargs={})
    trigger6 = Trigger(classpath="airflow.triggers.testing.SuccessTrigger6", kwargs={})
    session.add(trigger1)
    session.add(trigger2)
    session.add(trigger3)
    session.add(trigger4)
    session.add(trigger5)
    session.add(trigger6)
    session.commit()
    assert session.query(Trigger).count() == 6
    # Tie one to a fake TaskInstance that is not deferred, and one to one that is
    task_instance = create_task_instance(
        session=session, task_id="fake", state=State.DEFERRED, logical_date=timezone.utcnow()
    )
    task_instance.trigger_id = trigger1.id
    session.add(task_instance)
    fake_task1 = EmptyOperator(task_id="fake2", dag=task_instance.task.dag)
    task_instance1 = TaskInstance(
        task=fake_task1, run_id=task_instance.run_id, dag_version_id=task_instance.dag_version_id
    )
    task_instance1.state = State.SUCCESS
    task_instance1.trigger_id = trigger2.id
    session.add(task_instance1)
    fake_task2 = EmptyOperator(task_id="fake3", dag=task_instance.task.dag)
    task_instance2 = TaskInstance(
        task=fake_task2, run_id=task_instance.run_id, dag_version_id=task_instance.dag_version_id
    )
    task_instance2.state = State.SUCCESS
    task_instance2.trigger_id = trigger4.id
    session.add(task_instance2)
    session.commit()

    # Create assets
    asset = AssetModel("test")
    asset.triggers.extend([trigger4, trigger5])
    session.add(asset)
    session.commit()
    assert session.query(AssetModel).count() == 1

    # Create deadline with trigger
    deadline = Deadline(deadline_time=DEFAULT_DATE, callback="classpath.callback")
    deadline.trigger = trigger6
    session.add(deadline)
    session.commit()

    # Run clear operation
    Trigger.clean_unused()
    results = session.query(Trigger).all()
    assert len(results) == 4
    assert {result.id for result in results} == {trigger1.id, trigger4.id, trigger5.id, trigger6.id}


@patch.object(Deadline, "handle_callback_event")
def test_submit_event(mock_deadline_submit_event, session, create_task_instance):
    """
    Tests that events submitted to a trigger re-wake their dependent
    task instances and notify associated assets and deadlines.
    """
    # Make a trigger
    trigger = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    session.add(trigger)
    # Make a TaskInstance that's deferred and waiting on it
    task_instance = create_task_instance(
        session=session, logical_date=timezone.utcnow(), state=State.DEFERRED
    )
    task_instance.trigger_id = trigger.id
    task_instance.next_kwargs = {"cheesecake": True}
    # Create assets
    asset = AssetModel("test")
    asset.triggers.extend([trigger])
    session.add(asset)

    # Create a deadline with the same trigger
    deadline = Deadline(deadline_time=DEFAULT_DATE, callback="classpath.callback")
    deadline.trigger = trigger
    session.add(deadline)
    session.commit()

    # Check that the asset has 0 event prior to sending an event to the trigger
    assert session.query(AssetEvent).filter_by(asset_id=asset.id).count() == 0

    # Create event
    payload = "payload"
    event = TriggerEvent(payload)
    # Call submit_event
    Trigger.submit_event(trigger.id, event, session=session)
    # commit changes made by submit event and expire all cache to read from db.
    session.flush()
    # Check that the task instance is now scheduled
    updated_task_instance = session.query(TaskInstance).one()
    assert updated_task_instance.state == State.SCHEDULED
    assert updated_task_instance.next_kwargs == {"event": payload, "cheesecake": True}
    # Check that the asset has received an event
    assert session.query(AssetEvent).filter_by(asset_id=asset.id).count() == 1
    asset_event = session.query(AssetEvent).filter_by(asset_id=asset.id).first()
    assert asset_event.extra == {"from_trigger": True, "payload": payload}

    # Check that the deadline's handle_callback_event was called
    mock_deadline_submit_event.assert_called_once_with(event, session)


def test_submit_failure(session, create_task_instance):
    """
    Tests that failures submitted to a trigger fail their dependent
    task instances if not using a TaskEndEvent.
    """
    # Make a trigger
    trigger = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    session.add(trigger)
    # Make a TaskInstance that's deferred and waiting on it
    task_instance = create_task_instance(task_id="fake", logical_date=timezone.utcnow(), state=State.DEFERRED)
    task_instance.trigger_id = trigger.id
    session.commit()
    # Call submit_event
    Trigger.submit_failure(trigger.id, session=session)
    # Check that the task instance is now scheduled to fail
    updated_task_instance = session.query(TaskInstance).one()
    assert updated_task_instance.state == State.SCHEDULED
    assert updated_task_instance.next_method == "__fail__"


@pytest.mark.parametrize(
    "event_cls, expected",
    [
        (TaskSuccessEvent, "success"),
        (TaskFailedEvent, "failed"),
        (TaskSkippedEvent, "skipped"),
    ],
)
@patch("airflow._shared.timezones.timezone.utcnow")
def test_submit_event_task_end(mock_utcnow, session, create_task_instance, event_cls, expected):
    """
    Tests that events inheriting BaseTaskEndEvent *don't* re-wake their dependent
    but mark them in the appropriate terminal state and send xcom
    """
    now = pendulum.now("UTC")
    mock_utcnow.return_value = now

    # Make a trigger
    trigger = Trigger(classpath="does.not.matter", kwargs={})
    session.add(trigger)
    # Make a TaskInstance that's deferred and waiting on it
    task_instance = create_task_instance(
        session=session, logical_date=timezone.utcnow(), state=State.DEFERRED
    )
    task_instance.trigger_id = trigger.id
    session.commit()

    def get_xcoms(ti):
        return XComModel.get_many(dag_ids=[ti.dag_id], task_ids=[ti.task_id], run_id=ti.run_id).all()

    # now for the real test
    # first check initial state
    ti: TaskInstance = session.query(TaskInstance).one()
    assert ti.state == "deferred"
    assert get_xcoms(ti) == []

    session.flush()
    # now, for each type, submit event
    # verify that (1) task ends in right state and (2) xcom is pushed
    Trigger.submit_event(
        trigger.id, event_cls(xcoms={"return_value": "xcomret", "a": "b", "c": "d"}), session=session
    )
    # commit changes made by submit event and expire all cache to read from db.
    session.flush()
    # Check that the task instance is now correct
    ti = session.query(TaskInstance).one()
    assert ti.state == expected
    assert ti.next_kwargs is None
    assert ti.end_date == now
    assert ti.duration is not None
    actual_xcoms = {x.key: x.value for x in get_xcoms(ti)}
    expected_xcoms = {}
    for k, v in {"return_value": "xcomret", "a": "b", "c": "d"}.items():
        expected_xcoms[k] = json.dumps(v)
    assert actual_xcoms == expected_xcoms


@pytest.mark.need_serialized_dag
def test_assign_unassigned(session, create_task_instance):
    """
    Tests that unassigned triggers of all appropriate states are assigned.
    """
    time_now = timezone.utcnow()
    triggerer_heartrate = 10
    finished_triggerer = Job(heartrate=triggerer_heartrate, state=State.SUCCESS)
    TriggererJobRunner(finished_triggerer)
    finished_triggerer.end_date = time_now - datetime.timedelta(hours=1)
    session.add(finished_triggerer)
    assert not finished_triggerer.is_alive()
    healthy_triggerer = Job(heartrate=triggerer_heartrate, state=State.RUNNING)
    TriggererJobRunner(healthy_triggerer)
    session.add(healthy_triggerer)
    assert healthy_triggerer.is_alive()
    new_triggerer = Job(heartrate=triggerer_heartrate, state=State.RUNNING)
    TriggererJobRunner(new_triggerer)
    session.add(new_triggerer)
    assert new_triggerer.is_alive()
    # This trigger's last heartbeat is older than the check threshold, expect
    # its triggers to be taken by other healthy triggerers below
    unhealthy_triggerer = Job(
        heartrate=triggerer_heartrate,
        state=State.RUNNING,
        latest_heartbeat=time_now - datetime.timedelta(seconds=100),
    )
    TriggererJobRunner(unhealthy_triggerer)
    session.add(unhealthy_triggerer)
    # Triggerer is not healtht, its last heartbeat was too long ago
    assert not unhealthy_triggerer.is_alive()
    session.commit()
    trigger_on_healthy_triggerer = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    trigger_on_healthy_triggerer.triggerer_id = healthy_triggerer.id
    session.add(trigger_on_healthy_triggerer)
    ti_trigger_on_healthy_triggerer = create_task_instance(
        task_id="ti_trigger_on_healthy_triggerer",
        logical_date=time_now,
        run_id="trigger_on_healthy_triggerer_run_id",
    )
    ti_trigger_on_healthy_triggerer.trigger_id = trigger_on_healthy_triggerer.id
    session.add(ti_trigger_on_healthy_triggerer)
    trigger_on_unhealthy_triggerer = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    trigger_on_unhealthy_triggerer.triggerer_id = unhealthy_triggerer.id
    session.add(trigger_on_unhealthy_triggerer)
    ti_trigger_on_unhealthy_triggerer = create_task_instance(
        task_id="ti_trigger_on_unhealthy_triggerer",
        logical_date=time_now + datetime.timedelta(hours=1),
        run_id="trigger_on_unhealthy_triggerer_run_id",
    )
    ti_trigger_on_unhealthy_triggerer.trigger_id = trigger_on_unhealthy_triggerer.id
    session.add(ti_trigger_on_unhealthy_triggerer)
    trigger_on_killed_triggerer = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    trigger_on_killed_triggerer.triggerer_id = finished_triggerer.id
    session.add(trigger_on_killed_triggerer)
    ti_trigger_on_killed_triggerer = create_task_instance(
        task_id="ti_trigger_on_killed_triggerer",
        logical_date=time_now + datetime.timedelta(hours=2),
        run_id="trigger_on_killed_triggerer_run_id",
    )
    ti_trigger_on_killed_triggerer.trigger_id = trigger_on_killed_triggerer.id
    session.add(ti_trigger_on_killed_triggerer)
    trigger_unassigned_to_triggerer = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    session.add(trigger_unassigned_to_triggerer)
    ti_trigger_unassigned_to_triggerer = create_task_instance(
        task_id="ti_trigger_unassigned_to_triggerer",
        logical_date=time_now + datetime.timedelta(hours=3),
        run_id="trigger_unassigned_to_triggerer_run_id",
    )
    ti_trigger_unassigned_to_triggerer.trigger_id = trigger_unassigned_to_triggerer.id
    session.add(ti_trigger_unassigned_to_triggerer)
    assert trigger_unassigned_to_triggerer.triggerer_id is None
    session.commit()
    assert session.query(Trigger).count() == 4
    Trigger.assign_unassigned(new_triggerer.id, 100, health_check_threshold=30)
    session.expire_all()
    # Check that trigger on killed triggerer and unassigned trigger are assigned to new triggerer
    assert (
        session.query(Trigger).filter(Trigger.id == trigger_on_killed_triggerer.id).one().triggerer_id
        == new_triggerer.id
    )
    assert (
        session.query(Trigger).filter(Trigger.id == trigger_unassigned_to_triggerer.id).one().triggerer_id
        == new_triggerer.id
    )
    # Check that trigger on healthy triggerer still assigned to existing triggerer
    assert (
        session.query(Trigger).filter(Trigger.id == trigger_on_healthy_triggerer.id).one().triggerer_id
        == healthy_triggerer.id
    )
    # Check that trigger on unhealthy triggerer is assigned to new triggerer
    assert (
        session.query(Trigger).filter(Trigger.id == trigger_on_unhealthy_triggerer.id).one().triggerer_id
        == new_triggerer.id
    )


@pytest.mark.need_serialized_dag
def test_get_sorted_triggers_same_priority_weight(session, create_task_instance):
    """
    Tests that triggers are sorted by the creation_date if they have the same priority.
    """
    old_logical_date = datetime.datetime(
        2023, 5, 9, 12, 16, 14, 474415, tzinfo=pytz.timezone("Africa/Abidjan")
    )
    trigger_old = Trigger(
        classpath="airflow.triggers.testing.SuccessTrigger",
        kwargs={},
        created_date=old_logical_date + datetime.timedelta(seconds=30),
    )
    session.add(trigger_old)
    TI_old = create_task_instance(
        task_id="old",
        logical_date=old_logical_date,
        run_id="old_run_id",
    )
    TI_old.priority_weight = 1
    TI_old.trigger_id = trigger_old.id
    session.add(TI_old)

    new_logical_date = datetime.datetime(
        2023, 5, 9, 12, 17, 14, 474415, tzinfo=pytz.timezone("Africa/Abidjan")
    )
    trigger_new = Trigger(
        classpath="airflow.triggers.testing.SuccessTrigger",
        kwargs={},
        created_date=new_logical_date + datetime.timedelta(seconds=30),
    )
    session.add(trigger_new)
    TI_new = create_task_instance(
        task_id="new",
        logical_date=new_logical_date,
        run_id="new_run_id",
    )
    TI_new.priority_weight = 1
    TI_new.trigger_id = trigger_new.id
    session.add(TI_new)
    trigger_orphan = Trigger(
        classpath="airflow.triggers.testing.TriggerOrphan",
        kwargs={},
        created_date=new_logical_date,
    )
    session.add(trigger_orphan)
    trigger_asset = Trigger(
        classpath="airflow.triggers.testing.TriggerAsset",
        kwargs={},
        created_date=new_logical_date,
    )
    session.add(trigger_asset)
    trigger_deadline = Trigger(
        classpath="airflow.triggers.testing.TriggerDeadline",
        kwargs={},
        created_date=new_logical_date,
    )
    session.add(trigger_deadline)
    session.commit()
    assert session.query(Trigger).count() == 5
    # Create assets
    asset = AssetModel("test")
    asset.triggers.extend([trigger_asset])
    session.add(asset)
    # Create deadline with trigger
    deadline = Deadline(deadline_time=DEFAULT_DATE, callback="classpath.callback")
    deadline.trigger = trigger_deadline
    session.add(deadline)
    session.commit()

    trigger_ids_query = Trigger.get_sorted_triggers(capacity=100, alive_triggerer_ids=[], session=session)

    # Deadline triggers should be first, followed by task triggers, then asset triggers
    assert trigger_ids_query == [
        (trigger_deadline.id,),
        (trigger_old.id,),
        (trigger_new.id,),
        (trigger_asset.id,),
    ]


@pytest.mark.need_serialized_dag
def test_get_sorted_triggers_different_priority_weights(session, create_task_instance):
    """
    Tests that triggers are sorted by the priority_weight.
    """
    old_logical_date = datetime.datetime(
        2023, 5, 9, 12, 16, 14, 474415, tzinfo=pytz.timezone("Africa/Abidjan")
    )
    trigger_old = Trigger(
        classpath="airflow.triggers.testing.SuccessTrigger",
        kwargs={},
        created_date=old_logical_date + datetime.timedelta(seconds=30),
    )
    session.add(trigger_old)
    TI_old = create_task_instance(
        task_id="old",
        logical_date=old_logical_date,
        run_id="old_run_id",
    )
    TI_old.priority_weight = 1
    TI_old.trigger_id = trigger_old.id
    session.add(TI_old)

    new_logical_date = datetime.datetime(
        2023, 5, 9, 12, 17, 14, 474415, tzinfo=pytz.timezone("Africa/Abidjan")
    )
    trigger_new = Trigger(
        classpath="airflow.triggers.testing.SuccessTrigger",
        kwargs={},
        created_date=new_logical_date + datetime.timedelta(seconds=30),
    )
    session.add(trigger_new)
    TI_new = create_task_instance(
        task_id="new",
        logical_date=new_logical_date,
        run_id="new_run_id",
    )
    TI_new.priority_weight = 2
    TI_new.trigger_id = trigger_new.id
    session.add(TI_new)

    session.commit()
    assert session.query(Trigger).count() == 2

    trigger_ids_query = Trigger.get_sorted_triggers(capacity=100, alive_triggerer_ids=[], session=session)

    assert trigger_ids_query == [(trigger_new.id,), (trigger_old.id,)]


class SensitiveKwargsTrigger(BaseTrigger):
    """
    A trigger that has sensitive kwargs.
    """

    def __init__(self, param1: str, param2: str):
        super().__init__()
        self.param1 = param1
        self.param2 = param2

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "unit.models.test_trigger.SensitiveKwargsTrigger",
            {
                "param1": self.param1,
                "param2": self.param2,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        yield TriggerEvent({})


@conf_vars({("core", "fernet_key"): Fernet.generate_key().decode()})
def test_serialize_sensitive_kwargs():
    """
    Tests that sensitive kwargs are encrypted.
    """
    trigger_instance = SensitiveKwargsTrigger(param1="value1", param2="value2")
    trigger_row: Trigger = Trigger.from_object(trigger_instance)

    assert trigger_row.kwargs["param1"] == "value1"
    assert trigger_row.kwargs["param2"] == "value2"
    assert isinstance(trigger_row.encrypted_kwargs, str)
    assert "value1" not in trigger_row.encrypted_kwargs
    assert "value2" not in trigger_row.encrypted_kwargs


def test_kwargs_not_encrypted():
    """
    Tests that we don't decrypt kwargs if they aren't encrypted.
    We weren't able to encrypt the kwargs in all migration paths.
    """
    trigger = Trigger(classpath="airflow.triggers.testing.SuccessTrigger", kwargs={})
    # force the `encrypted_kwargs` to be unencrypted, like they would be after an offline upgrade
    trigger.encrypted_kwargs = json.dumps(
        BaseSerialization.serialize({"param1": "value1", "param2": "value2"})
    )

    assert trigger.kwargs["param1"] == "value1"
    assert trigger.kwargs["param2"] == "value2"
