#!/usr/bin/env python
# coding: utf-8

from __future__ import absolute_import, division, print_function, unicode_literals

import itertools
import json
import os
import sys
import time
import typing
import unittest
from uuid import uuid4

import boto3

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))  # noqa
sys.path.insert(0, pkg_root)  # noqa

from dss.events import chunkedtask
from dss.events.chunkedtask import awsconstants
from dss.events.chunkedtask._awsimpl import AWS_FAST_TEST_CLIENT_NAME


class TestChunkedTaskRuntime(chunkedtask.Runtime[tuple]):
    def __init__(self, initial_time_millis: int, tick_iterator: typing.Iterator[int]) -> None:
        self.remaining_time = initial_time_millis
        self.tick_iterator = tick_iterator
        self.rescheduled_state = None  # type: typing.Optional[tuple]

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_time

    def schedule_work(self, state: tuple):
        # it's illegal for there to be no state.
        assert state is not None
        self.rescheduled_state = state

    def advance_time(self):
        self.remaining_time -= self.tick_iterator.__next__()

    def get_rescheduled_state(self) -> tuple:
        return self.rescheduled_state


class TestChunkedTask(chunkedtask.Task):
    def __init__(
            self,
            state: tuple,
            runtime: TestChunkedTaskRuntime,
            expected_max_one_unit_runtime_millis: int) -> None:
        self.x0, self.x1, self.rounds_remaining = state
        self.runtime = runtime
        self._expected_max_one_unit_runtime_millis = expected_max_one_unit_runtime_millis

    @property
    def expected_max_one_unit_runtime_millis(self) -> int:
        return self._expected_max_one_unit_runtime_millis

    def get_state(self) -> typing.Any:
        return self.x0, self.x1, self.rounds_remaining

    def run_one_unit(self) -> bool:
        x0new = self.x0 + self.x1
        self.x1 = self.x0
        self.x0 = x0new
        self.runtime.advance_time()

        self.rounds_remaining -= 1

        return self.rounds_remaining > 0


class TestChunkedTaskRunner(unittest.TestCase):
    def test_workload_resumes(self):
        initial_state = (1, 1, 25)
        expected_max_one_unit_runtime_millis = 10  # we know exactly how long we'll take.  we're so good at guessing!
        tick_iterator = itertools.repeat(10)
        initial_time_millis = 100

        current_state = initial_state

        serialize_count = 0
        while True:
            env = TestChunkedTaskRuntime(initial_time_millis, tick_iterator)
            task = TestChunkedTask(current_state, env, expected_max_one_unit_runtime_millis)
            runner = chunkedtask.Runner(task, env)

            runner.run()

            rescheduled_state = env.get_rescheduled_state()
            if rescheduled_state is None:
                # we're done!
                final_state = task.get_state()
                self.assertEqual(final_state, (196418, 121393, 0))
                self.assertEqual(serialize_count, 2)
                break
            else:
                serialize_count += 1
                current_state = rescheduled_state


class TestAWSChunkedTask(unittest.TestCase):
    def test_fast(self):
        uuid = uuid4()
        payload = {
            awsconstants.CLIENT_KEY: AWS_FAST_TEST_CLIENT_NAME,
            awsconstants.STATE_KEY: (str(uuid), 0, 5),
        }

        sts_client = boto3.client('sts')
        accountid = sts_client.get_caller_identity()['Account']

        sns_client = boto3.client('sns')
        region = os.environ['AWS_DEFAULT_REGION']
        topic = awsconstants.get_worker_sns_topic()
        arn = f"arn:aws:sns:{region}:{accountid}:{topic}"
        sns_client.publish(
            TopicArn=arn,
            Message=json.dumps(payload),
        )

        logs_client = boto3.client('logs')
        starttime = time.time()
        while time.time() < starttime + 30:
            response = logs_client.filter_log_events(
                logGroupName="/aws/lambda/" + awsconstants.get_worker_sns_topic(),
                filterPattern="Completed task"
            )

            for event in response['events']:
                if event['message'].find(str(uuid)) != -1:
                    return

        self.fail("Did not find success marker in logs")


if __name__ == '__main__':
    unittest.main()