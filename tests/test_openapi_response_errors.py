"""Offline response-boundary regressions; existing fixture/tests stay unchanged."""

from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone
import http.client
import io
import json
import socket
import stat
import traceback
from typing import cast
import unittest
from unittest.mock import patch

import test_openapi


UNTRUSTED = "untrusted-response-marker"
FAULTS: dict[str, bytes | Callable[[], Exception]] = {
    "json": b'{"' + UNTRUSTED.encode(),
    "encoding": UNTRUSTED.encode() + b"\xff",
    "oversized": UNTRUSTED.encode() * 100,
    "nested_json": b"[" * 1100 + b"{malformed}",
    "short_body": b"{}",
    "short_empty_body": b"",
    "incomplete": lambda: http.client.IncompleteRead(UNTRUSTED.encode(), 99),
    "disconnected": lambda: http.client.RemoteDisconnected(UNTRUSTED),
    "bad_status": lambda: http.client.BadStatusLine(UNTRUSTED),
    "reset": lambda: ConnectionResetError(UNTRUSTED),
    "oserror": lambda: OSError(UNTRUSTED),
}


class ResponseErrorTests(unittest.TestCase):
    def setUp(self):
        # Composition avoids collecting the existing TestCase a second time.
        self.f = test_openapi.OpenApiTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.cli = self.f.cli

    def boundary(self, fault: bytes | Callable[[], Exception]):
        if not callable(fault):
            return nullcontext()

        def read(response, size=-1):
            if response.url.endswith(("/complete", "/task", "/focus")):
                raise fault()
            return response.fp.read(size)

        return patch("urllib.response.addinfourl.read", read, create=True)

    def assert_review_only(self, kind, sent):
        entries = self.cli["read_outbox"]()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["kind"], kind)
        self.assertTrue(entry["payload"]["needsReview"])
        self.assertEqual(entry["owner"], self.cli["session_owner"](self.f.session))
        self.assertEqual(entry["backend"], "openapi")
        for _ in range(2):
            self.assertEqual(self.cli["drain_outbox"](self.f.session),
                             {"sent": 0, "queued": 1, "dropped": 0})
        self.assertEqual(len(self.f.sent), sent)
        self.assertNotIn(UNTRUSTED, json.dumps(self.cli["read_outbox"]()))
        self.assertEqual(stat.S_IMODE(self.f.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.f.state / "outbox.json").stat().st_mode), 0o600)

    def exercise(self, action, name):
        fault = FAULTS[name]
        if name == "oversized":
            self.cli["MAX_BODY_BYTES"] = 1024
        task = self.f.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=2)
        prior = self.f.connected([task], pomoStats={"todayPomoCount": 2})
        if action in ("online", "replay"):
            self.f.respond("GET", "/project/p1/task/t1", task)
            path, kind = "/project/p1/task/t1/complete", "complete"
        else:
            path, kind = ("/task", "add") if action == "create" else ("/focus", "pomo")
        body = b"{}" if callable(fault) else fault
        headers = {"Content-Length": "99"} if name.startswith("short_") else None
        self.f.respond("POST", path, body, headers=headers)
        if action == "replay":
            self.cli["queue_write"]("complete", {"taskId": "t1", "expectedDueDate": task["dueDate"]},
                                    "offline", self.f.session)
            # Preserve the existing offline optimistic overlay, rather than
            # treating queue admission as part of the response boundary.
            prior = self.cli["_read_json"](self.cli["DATA_PATH"])
            with self.boundary(fault):
                self.assertEqual(self.cli["drain_outbox"](self.f.session)["queued"], 1)
        else:
            with self.boundary(fault), self.assertRaises(self.cli["TickTickError"]) as caught:
                if action == "online":
                    self.cli["set_task_status"]("t1", self.cli["STATUS_DONE"])
                elif action == "create":
                    self.cli["create_task"]("Dummy task", "p1", None)
                else:
                    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
                    self.cli["log_pomodoro"](now, now, 0, 0)
            self.assertTrue(caught.exception.needs_review)
            self.assertFalse(caught.exception.transient)
            self.assertNotIn(UNTRUSTED, "".join(traceback.format_exception(caught.exception)))
        self.assert_review_only(kind, 2 if action in ("online", "replay") else 1)
        self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"]), dict(prior, queued=1))
        self.assertEqual(self.f.responses, [])

    def test_incomplete_create_cli_is_sanitized(self):
        self.f.connected()
        self.f.respond("POST", "/task", {})
        with self.boundary(FAULTS["incomplete"]):
            code, out, err = self.f.command(["add", "Dummy", "--project", "p1"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("review", err)
        self.assertNotIn(UNTRUSTED, err)
        self.assertNotIn("Traceback", err)
        self.assert_review_only("add", 1)

    def test_actual_bounded_httpresponse_short_read_requires_review(self):
        self.f.connected()

        class Socket:
            def makefile(self, mode):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n{"id":"server-created"}')

        response = http.client.HTTPResponse(cast(socket.socket, Socket()))
        response.begin()
        # Unlike unbounded read(), read(limit) can return partial bytes
        # without raising IncompleteRead, even when the JSON looks complete.
        with patch("urllib.request.OpenerDirector.open", return_value=response) as attempted:
            with self.assertRaises(self.cli["TickTickError"]) as caught:
                self.cli["create_task"]("Dummy", "p1", None)
            self.assertTrue(caught.exception.needs_review)
            self.assert_review_only("add", 0)
            attempted.assert_called_once()

    def test_provider_rejection_is_not_response_uncertainty(self):
        for path in ("/task", "/focus", "/project/p1/task/t1/complete"):
            for body, status in (({"error": UNTRUSTED}, 200),
                                 ({"id2error": {"t1": UNTRUSTED}}, 200),
                                 ({"errorCode": UNTRUSTED}, 200),
                                 (UNTRUSTED.encode(), 400), (UNTRUSTED.encode(), 401),
                                 (UNTRUSTED.encode(), 403)):
                with self.subTest(path=path, body=body, status=status):
                    self.f.connected([self.f.task()])
                    self.f.respond("POST", path, body, status)
                    with self.assertRaises(self.cli["TickTickError"]) as caught:
                        if path.endswith("/complete"):
                            self.cli["openapi_status"](self.f.session,
                                self.f.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=1), 2)
                        else:
                            self.cli["openapi_create"](path, self.f.session, {})
                    self.assertFalse(caught.exception.needs_review)
                    self.assertFalse(caught.exception.transient)
                    self.assertNotIn(UNTRUSTED, str(caught.exception))
                    self.assertEqual(self.cli["read_outbox"](), [])

    def test_malformed_reads_and_idempotent_updates_do_not_need_review(self):
        for method, path in (("GET", "/project"), ("POST", "/task/t1")):
            self.f.respond(method, path, FAULTS["json"])
            with self.assertRaises(self.cli["TickTickError"]) as caught:
                self.cli["request"](method, path, self.f.session)
            self.assertFalse(caught.exception.needs_review)
            self.assertFalse(caught.exception.transient)
            self.assertNotIn(UNTRUSTED, "".join(traceback.format_exception(caught.exception)))


def _case(action, fault):
    def test(self):
        self.exercise(action, fault)
    test.__name__ = "test_" + action + "_" + fault + "_requires_review"
    return test


for _action in ("create", "focus", "online", "replay"):
    for _fault in FAULTS:
        _test = _case(_action, _fault)
        setattr(ResponseErrorTests, _test.__name__, _test)


if __name__ == "__main__":
    unittest.main()
