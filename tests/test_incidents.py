"""Incident logging and the retry loop that feeds it."""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_metadata_extractor as vme  # noqa: E402
from test_errors import FakeHTTPError  # noqa: E402


class RecordingLogger(vme.IncidentLogger):
    """Incident logger that keeps everything in memory."""

    def __init__(self):
        super().__init__(path=None, enabled=False, stream=io.StringIO())

    def lines(self):
        return [json.loads(line) for line in
                self.stream.getvalue().strip().splitlines() if line]


def failing(*errors):
    """An operation that raises the given errors in order, then returns 'ok'."""
    queue = list(errors)

    def operation():
        if queue:
            raise queue.pop(0)
        return "ok"

    return operation


class IncidentLogTests(unittest.TestCase):
    def test_record_shape_is_exactly_the_documented_schema(self):
        logger = RecordingLogger()
        logger.log(url="https://example.com/v", stage="metadata",
                   category=vme.Category.RATE_LIMIT, attempt=1, max_attempts=5,
                   incident_id="abc123", error="HTTP Error 429", http_status=429,
                   retry_in_seconds=3.14159)
        record = logger.lines()[0]
        self.assertEqual(list(record), list(vme.IncidentLogger.FIELDS))
        self.assertEqual(record["tool"], vme.TOOL_NAME)
        self.assertEqual(record["tool_version"], vme.TOOL_VERSION)
        self.assertEqual(record["retry_in_seconds"], 3.142)
        self.assertFalse(record["resolved"])
        self.assertTrue(record["timestamp"].endswith("Z"))

    def test_writes_jsonl_to_disk_and_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "incidents.jsonl")
            logger = vme.IncidentLogger(path)
            for attempt in (1, 2):
                logger.log(url="u", stage="metadata", category=vme.Category.TRANSIENT,
                           attempt=attempt, max_attempts=5, incident_id="i1")
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["attempt"] for line in lines], [1, 2])

    def test_disabled_logger_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "incidents.jsonl")
            logger = vme.IncidentLogger(path, enabled=False)
            logger.log(url="u", stage="metadata", category=vme.Category.BLOCKED,
                       attempt=1, max_attempts=2, incident_id="i1")
            self.assertFalse(os.path.exists(path))
            self.assertEqual(len(logger.records), 1)


class RetryLoopTests(unittest.TestCase):
    def setUp(self):
        self.incidents = RecordingLogger()
        self.delays = []
        self.policy = vme.RetryPolicy(retries=4, blocked_retries=1, base_backoff=2.0,
                                      max_backoff=300.0)

    def run_op(self, operation, policy=None):
        return vme.run_with_retries(
            operation, url="https://example.com/v", stage="metadata",
            policy=policy or self.policy, incidents=self.incidents,
            sleeper=self.delays.append, jitter=lambda: 1.0)

    def test_success_first_time_logs_nothing(self):
        self.assertEqual(self.run_op(lambda: "ok"), "ok")
        self.assertEqual(self.incidents.lines(), [])
        self.assertEqual(self.delays, [])

    def test_throttled_then_resolved(self):
        operation = failing(FakeHTTPError(429, "Too Many Requests"),
                            FakeHTTPError(429, "Too Many Requests"))
        self.assertEqual(self.run_op(operation), "ok")

        lines = self.incidents.lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([line["attempt"] for line in lines], [1, 2, 3])
        self.assertEqual([line["resolved"] for line in lines], [False, False, True])
        self.assertEqual({line["category"] for line in lines}, {vme.Category.RATE_LIMIT})
        self.assertEqual({line["http_status"] for line in lines}, {429})
        # One incident_id ties the whole episode together.
        self.assertEqual(len({line["incident_id"] for line in lines}), 1)
        # Exponential backoff, ceiling reached because jitter is stubbed to 1.0.
        self.assertEqual(self.delays, [2.0, 4.0])
        self.assertIsNone(lines[-1]["retry_in_seconds"])

    def test_unresolved_incidents_are_the_failure_list(self):
        operation = failing(*[FakeHTTPError(429) for _ in range(9)])
        with self.assertRaises(vme.ExtractionError) as caught:
            self.run_op(operation)
        lines = self.incidents.lines()
        self.assertEqual(len(lines), 5)                       # retries=4 -> 5 attempts
        self.assertTrue(all(not line["resolved"] for line in lines))
        self.assertEqual(caught.exception.category, vme.Category.RATE_LIMIT)
        self.assertEqual(caught.exception.attempts, 5)
        self.assertEqual(caught.exception.incident_id, lines[0]["incident_id"])

    def test_fatal_is_never_retried(self):
        operation = failing(RuntimeError("ERROR: Private video. Sign in if you've been granted access"))
        with self.assertRaises(vme.ExtractionError) as caught:
            self.run_op(operation)
        lines = self.incidents.lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["attempt"], 1)
        self.assertEqual(lines[0]["max_attempts"], 1)
        self.assertIsNone(lines[0]["retry_in_seconds"])
        self.assertEqual(caught.exception.category, vme.Category.FATAL)
        self.assertEqual(self.delays, [])

    def test_bot_check_gets_a_short_budget(self):
        operation = failing(*[RuntimeError("Sign in to confirm you're not a bot")
                              for _ in range(9)])
        with self.assertRaises(vme.ExtractionError):
            self.run_op(operation)
        lines = self.incidents.lines()
        self.assertEqual(len(lines), 2)                       # blocked_retries=1
        self.assertEqual(lines[0]["max_attempts"], 2)
        self.assertEqual({line["category"] for line in lines}, {vme.Category.BLOCKED})
        self.assertEqual(len(self.delays), 1)

    def test_retry_after_header_is_honoured(self):
        operation = failing(FakeHTTPError(429, headers={"Retry-After": "12"}))
        self.assertEqual(self.run_op(operation), "ok")
        self.assertEqual(self.delays, [12.0])
        self.assertEqual(self.incidents.lines()[0]["retry_in_seconds"], 12.0)

    def test_backoff_is_capped(self):
        policy = vme.RetryPolicy(retries=6, base_backoff=10.0, max_backoff=25.0)
        operation = failing(*[FakeHTTPError(503) for _ in range(3)])
        self.assertEqual(self.run_op(operation, policy), "ok")
        self.assertEqual(self.delays, [10.0, 20.0, 25.0])

    def test_stage_is_recorded(self):
        vme.run_with_retries(failing(FakeHTTPError(429)), url="u", stage="subtitles",
                             policy=self.policy, incidents=self.incidents,
                             sleeper=self.delays.append, jitter=lambda: 1.0)
        self.assertEqual({line["stage"] for line in self.incidents.lines()}, {"subtitles"})

    def test_mixed_failures_reclassify_per_attempt(self):
        operation = failing(FakeHTTPError(503), FakeHTTPError(429))
        self.assertEqual(self.run_op(operation), "ok")
        categories = [line["category"] for line in self.incidents.lines()]
        self.assertEqual(categories[:2], [vme.Category.TRANSIENT, vme.Category.RATE_LIMIT])


if __name__ == "__main__":
    unittest.main()
