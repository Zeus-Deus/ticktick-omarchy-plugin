"""Official API behavior with synthetic credentials and an offline HTTP transport."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email.message import Message
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import ssl
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import urllib.response


CLI = Path(__file__).resolve().parents[1] / "bin" / "omarchy-ticktick"
TOKEN = "dummy-official-access-token"


class OpenApiTests(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("ticktick_openapi_test", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        self.cli = vars(module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.cli["STATE_DIR"] = str(self.state)
        for key, name in (("TOKEN_PATH", "session.json"), ("DATA_PATH", "data.json"),
                          ("OUTBOX_PATH", "outbox.json"), ("DRAIN_LOCK_PATH", "drain.lock")):
            self.cli[key] = str(self.state / name)
        self.session = {"backend": "openapi", "source": "api-token", "token": TOKEN}
        self.responses = []
        self.sent = []
        self.contexts = []
        self.read_sizes = []
        owner = self

        class Body(io.BytesIO):
            def read(self, size=-1):
                owner.read_sizes.append(size)
                return super().read(size)

        def transport(handler, req):
            self.sent.append(req)
            self.contexts.append(handler._context)
            self.assertTrue(self.responses, "Unexpected HTTP request")
            method, path, status, headers, body = self.responses.pop(0)
            self.assertEqual(req.method, method)
            self.assertEqual(urllib.parse.urlsplit(req.full_url).path, "/open/v1" + path)
            message = Message()
            for key, value in headers.items():
                message[key] = value
            response = urllib.response.addinfourl(Body(body), message, req.full_url, status)
            response.msg = "Synthetic response"
            return response

        for target in ("HTTPSHandler.https_open", "HTTPHandler.http_open"):
            p = patch("urllib.request." + target, transport)
            p.start()
            self.addCleanup(p.stop)
        for target, kwargs in (("urllib.request.getproxies", {"return_value": {}}),
                               ("socket.socket.connect", {"side_effect": AssertionError("Network forbidden")})):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def respond(self, method, path, body=None, status=200, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
        self.responses.append((method, path, status, headers or {}, data))

    def write(self, key, value):
        self.cli["_write_private"](self.cli[key], value)

    def cache(self, tasks=None, **fields):
        data = {"cacheOwner": self.cli["session_owner"](self.session), "backend": "openapi",
                "tasks": tasks or [], "habits": [], "checkins": {}, "projects": [], **fields}
        self.write("DATA_PATH", data)
        return data

    def connected(self, tasks=None, **fields):
        self.write("TOKEN_PATH", self.session)
        return self.cache(tasks, **fields)

    def full_state(self):
        self.respond("POST", "/preference", {"timeZone": "Europe/Brussels"})
        self.respond("GET", "/project", [{"id": "p1", "name": "Work", "closed": False}])
        self.respond("GET", "/project/p1/data", {"project": {"id": "p1"}, "tasks": [self.task()]})
        self.respond("POST", "/task/filter", [self.task("inbox-task", projectId="actual-inbox-project")])
        self.respond("GET", "/tag", [{"name": "work", "label": "Work", "color": "#123456"}])
        self.respond("GET", "/habit", [{"id": "h1", "name": "Read", "goal": 3, "step": 1,
                                          "status": 73, "repeatRule": "RRULE:FREQ=DAILY"}])
        self.respond("GET", "/habit/checkins", [{"habitId": "h1", "year": 2026, "checkins": [
            {"stamp": 20261025, "time": "2026-10-25T08:00:00+0000", "value": 3, "goal": 3, "status": 919}]}])
        self.respond("GET", "/focus", [{"id": "f1", "type": 0, "endTime": "2026-10-25T09:25:00+0000",
                                         "duration": 1500, "status": 987}])
        self.respond("GET", "/focus", [{"id": "f2", "type": 1, "endTime": "2026-10-25T10:30:00+0000", "duration": 300}])

    def task(self, task_id="t1", **fields):
        return {"id": task_id, "projectId": "p1", "title": "Read", "status": 0,
                "priority": 3, "tags": ["work"], "isAllDay": True,
                "startDate": "2026-10-25T00:00:00.000+0000", "dueDate": "2026-10-25T00:00:00.000+0000",
                "timeZone": "Europe/Brussels", "content": "Notes", "items": [], **fields}

    def command(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            code = self.cli["main"](argv)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_bearer_transport_is_pinned_private_and_bounded(self):
        self.respond("GET", "/project", [])
        self.assertEqual(self.cli["request"]("GET", "/project", self.session, timeout=7), [])
        req = self.sent[0]
        self.assertEqual(req.full_url, "https://api.ticktick.com/open/v1/project")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertIsNone(req.get_header("Cookie"))
        self.assertIsNone(req.get_header("X-device"))
        self.assertEqual(req.timeout, 7)
        self.assertTrue(self.contexts[0].check_hostname)
        self.assertEqual(self.contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(self.read_sizes, [self.cli["MAX_BODY_BYTES"] + 1])

    def test_official_rejects_legacy_paths_and_unsafe_destination(self):
        for base, path in (("http://api.ticktick.com/open/v1", "/project"),
                           ("https://different.invalid/open/v1", "/project"),
                           ("https://api.ticktick.com/open/v1", "/batch/check/0"),
                           ("https://api.ticktick.com/open/v1", "/project/../../api/v2")):
            self.cli["OPEN_API"] = base
            with self.assertRaises(self.cli["TickTickError"]):
                self.cli["request"]("GET", path, self.session)
        self.assertEqual(self.sent, [])

    def test_redirect_never_receives_bearer(self):
        for status in (301, 302, 303, 307, 308):
            self.respond("POST", "/task", b"", status, {"Location": "https://different.invalid/collect"})
            with self.assertRaisesRegex(self.cli["TickTickError"], "redirect refused"):
                self.cli["request"]("POST", "/task", self.session, payload={"title": "Test"})
        self.assertEqual(len(self.sent), 5)

    def test_safe_auth_rate_limit_and_payload_errors(self):
        for status, body, auth, transient in ((401, TOKEN.encode(), True, False),
                                               (401, {"errorCode": "unlisted_provider_code"}, True, False),
                                               (403, b"", True, False), (429, b"", False, True),
                                               (500, TOKEN.encode(), False, True),
                                               (200, {"id2error": {"t1": TOKEN}}, False, False)):
            self.respond("GET", "/project", body, status)
            with self.assertRaises(self.cli["TickTickError"]) as caught:
                self.cli["request"]("GET", "/project", self.session)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(caught.exception.auth_required, auth)
            self.assertEqual(caught.exception.transient, transient)

    def test_full_sync_normalizes_actual_habit_and_focus_fields(self):
        self.full_state()
        state = self.cli["fetch_state"](self.session)
        self.assertEqual({t["id"] for t in state["tasks"]}, {"t1", "inbox-task"})
        self.assertEqual(state["inboxId"], "")
        self.assertEqual(state["habits"][0]["status"], 73)
        entry = state["checkins"]["h1"][0]
        self.assertEqual(entry["checkinStamp"], 20261025)
        self.assertEqual(entry["checkinTime"], "2026-10-25T08:00:00+0000")
        self.assertEqual(entry["providerStatus"], 919)
        self.assertEqual(entry["status"], self.cli["CHECKIN_DONE"])
        self.assertEqual(state["pomoStats"], {"todayPomoCount": 1, "todayPomoDuration": 30})
        self.assertEqual(state["pomoPrefs"], {})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.sent[-3].full_url).query)
        self.assertEqual(query["habitIds"], ["h1"])
        self.assertIn("from", query)
        self.assertIn("to", query)
        self.assertEqual(self.responses, [])
        self.assertTrue(state["capabilities"]["recurringCompletion"])

    def test_scoped_sync_preserves_only_same_connection_cache(self):
        self.connected([self.task()], pomoPrefs={"pomoTime": 42}, pomoStats={"todayPomoCount": 5})
        self.respond("POST", "/preference", {"timeZone": "Europe/Brussels"})
        self.respond("GET", "/habit", [])
        state = self.cli["fetch_state"](self.session, "habits")
        self.assertEqual(len(state["tasks"]), 1)
        self.assertEqual(state["pomoPrefs"]["pomoTime"], 42)
        other = dict(self.session, token="different-dummy-token")
        self.respond("POST", "/preference", {"timeZone": "Europe/Brussels"})
        self.respond("GET", "/habit", [])
        state = self.cli["fetch_state"](other, "habits")
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["pomoStats"], {})

    def test_api_token_stdin_verifies_before_private_persistence(self):
        self.full_state()
        code, out, err = self.command(["login", "--api-token-stdin"], TOKEN + "\n")
        self.assertEqual((code, err), (0, ""))
        session = self.cli["load_session"]()
        self.assertEqual((session["backend"], session["source"], session["token"]), ("openapi", "api-token", TOKEN))
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for name in ("session.json", "data.json"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, (self.state / "data.json").read_text())

    def test_failed_login_preserves_prior_session_and_cache(self):
        prior = self.connected([self.task()])
        self.respond("POST", "/preference", b"Rejected " + TOKEN.encode(), 401)
        code, _, _ = self.command(["login", "--api-token-stdin"], "new-dummy-token\n")
        self.assertEqual(code, 3)
        self.assertEqual(self.cli["load_session"](), self.session)
        self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"]), prior)

    def test_failed_cache_storage_does_not_publish_new_credential(self):
        prior = self.connected([self.task()])
        original = self.cli["_write_private"]

        def write(path, value):
            if path == self.cli["DATA_PATH"] and value != prior:
                raise OSError("Synthetic disk failure")
            original(path, value)

        self.cli["_write_private"] = write
        self.full_state()
        code, _, err = self.command(["login", "--api-token-stdin"], "other-dummy-token\n")
        self.assertEqual(code, 1)
        self.assertIn("Cannot save", err)
        self.assertEqual(self.cli["load_session"](), self.session)
        self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"]), prior)

    def test_pending_legacy_migration_is_blocked_before_http(self):
        legacy = {"token": "dummy-cookie"}
        self.write("TOKEN_PATH", legacy)
        self.write("DATA_PATH", {"tasks": [self.task()]})
        self.cli["write_outbox"]([{"kind": "update", "payload": {"taskId": "t1", "fields": {"title": "Pending"}}}])
        snapshots = [(self.state / name).read_bytes() for name in ("session.json", "data.json", "outbox.json")]
        code, _, err = self.command(["login", "--api-token-stdin"], TOKEN + "\n")
        self.assertEqual(code, 1)
        self.assertIn("Pending changes", err)
        self.assertEqual(snapshots, [(self.state / name).read_bytes() for name in ("session.json", "data.json", "outbox.json")])
        self.assertEqual(self.sent, [])

    def test_malformed_official_token_stdin_is_rejected_before_http(self):
        for token in ("", "x" * 4097, "dummy-token\x00", "dummy-token\u0100"):
            code, _, _ = self.command(["login", "--api-token-stdin"], token + "\n")
            self.assertEqual(code, 1)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.state / "session.json").exists())

    def test_same_token_reconnect_keeps_pending_cache_overlay(self):
        self.connected([self.task()])
        self.cli["queue_write"]("update", {"taskId": "t1", "fields": {"title": "Pending"}}, "offline", self.session)
        self.full_state()
        code, _, _ = self.command(["login", "--api-token-stdin"], TOKEN + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(self.cli["cached_task"]("t1")["title"], "Pending")
        self.assertEqual(len(self.cli["read_outbox"]()), 1)

    def test_queue_after_connection_change_cannot_bind_to_new_account(self):
        self.connected([self.task()])
        self.write("TOKEN_PATH", dict(self.session, token="new-dummy-token"))
        with self.assertRaisesRegex(self.cli["TickTickError"], "connection changed"):
            self.cli["queue_write"]("update", {"taskId": "t1", "fields": {"title": "Pending"}}, "offline", self.session)
        self.assertEqual(self.cli["read_outbox"](), [])

    def test_different_account_and_backend_queues_cannot_replay_or_logout(self):
        self.connected([self.task()])
        self.cli["write_outbox"]([{"kind": "complete", "payload": {"taskId": "t1"},
                                    "backend": "openapi", "owner": "different-connection"}])
        with self.assertRaisesRegex(self.cli["TickTickError"], "different TickTick connection"):
            self.cli["drain_outbox"](self.session)
        code, _, _ = self.command(["logout"])
        self.assertEqual(code, 1)
        self.assertTrue((self.state / "session.json").exists())
        self.assertEqual(len(self.cli["read_outbox"]()), 1)
        self.assertEqual(self.sent, [])

    def test_stale_sync_cannot_overwrite_new_account_cache(self):
        self.connected([self.task()])
        old = self.cli["_read_json"](self.cli["DATA_PATH"])
        self.write("TOKEN_PATH", dict(self.session, token="other-dummy-token"))
        with self.assertRaisesRegex(self.cli["TickTickError"], "stale cache"):
            self.cli["write_cache"](old)

    def test_complete_reopen_and_delete_use_official_routes(self):
        task = self.task()
        self.connected([task])
        for status in (2, 0):
            self.respond("GET", "/project/p1/task/t1", task)
            self.respond("POST", "/project/p1/task/t1/complete" if status == 2 else "/task/t1", b"")
            self.assertEqual(self.cli["set_task_status"]("t1", status)["status"], status)
            if status == 0:
                self.assertEqual(json.loads(self.sent[-1].data), {"id": "t1", "projectId": "p1", "status": 0})
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("DELETE", "/project/p1/task/t1", b"")
        self.cli["delete_task"]("t1")

    def test_recurring_official_completion_delegates_both_modes_to_ticktick(self):
        for mode in (1, 2):
            with self.subTest(mode=mode):
                task = self.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=mode)
                self.connected([task])
                self.respond("GET", "/project/p1/task/t1", task)
                self.respond("POST", "/project/p1/task/t1/complete", b"")
                with patch.dict(self.cli, {"recurring_completion_plan": Mock(side_effect=AssertionError("Legacy rollover forbidden"))}):
                    self.assertEqual(self.cli["set_task_status"]("t1", 2)["status"], 2)
                self.assertEqual(self.sent[-1].method, "POST")
                self.assertTrue(self.sent[-1].full_url.endswith("/complete"))

    def test_uncertain_recurring_completion_is_retained_without_replay(self):
        task = self.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=1)
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("POST", "/project/p1/task/t1/complete", {}, status=503)
        with self.assertRaisesRegex(self.cli["TickTickError"], "review"):
            self.cli["set_task_status"]("t1", 2)
        pending = self.cli["read_outbox"]()
        self.assertTrue(pending[0]["payload"]["needsReview"])
        self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"][0]["status"], 0)
        sent = len(self.sent)
        self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
        self.assertEqual(len(self.sent), sent)

    def test_offline_recurring_replay_does_not_complete_a_different_occurrence(self):
        task = self.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=1)
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", {}, status=503)
        self.assertTrue(self.cli["set_task_status"]("t1", 2)["queued"])
        pending = self.cli["read_outbox"]()
        self.assertEqual(pending[0]["payload"]["expectedDueDate"], task["dueDate"])
        self.respond("GET", "/project/p1/task/t1", dict(task, dueDate="2026-10-26T00:00:00+0000"))
        self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
        self.assertEqual(self.sent[-1].method, "GET")

    def test_offline_recurring_replay_completes_only_the_original_occurrence(self):
        task = self.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=2)
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", {}, status=503)
        with patch.dict(self.cli, {"recurring_completion_plan": Mock(side_effect=AssertionError("Legacy rollover forbidden"))}):
            self.assertTrue(self.cli["set_task_status"]("t1", 2)["queued"])
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("POST", "/project/p1/task/t1/complete", b"")
        self.assertEqual(self.cli["drain_outbox"](self.session)["sent"], 1)
        self.assertEqual(self.cli["read_outbox"](), [])

    def test_uncertain_recurring_replay_is_never_automatically_repeated(self):
        task = self.task(repeatFlag="RRULE:FREQ=DAILY", repeatFrom=1)
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", {}, status=503)
        self.assertTrue(self.cli["set_task_status"]("t1", 2)["queued"])
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("POST", "/project/p1/task/t1/complete", {}, status=503)
        self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
        self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])
        sent = len(self.sent)
        self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
        self.assertEqual(len(self.sent), sent)

    def test_completion_safety_never_reads_an_ambient_session(self):
        task = self.task(repeatFlag="ERULE:LUNAR")
        ambient = Mock(side_effect=AssertionError("Unexpected ambient credential read"))
        with patch.dict(self.cli, {"cached_task": lambda _id: task, "load_session": ambient}):
            with self.assertRaisesRegex(self.cli["TickTickError"], "cannot be rolled forward"):
                self.cli["set_task_status"]("t1", 2)
        ambient.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_unsupported_official_repeat_rule_is_refused_without_write(self):
        task = self.task(repeatFlag="ERULE:LUNAR")
        self.connected([task])
        with self.assertRaisesRegex(self.cli["TickTickError"], "cannot be rolled forward"):
            self.cli["set_task_status"]("t1", 2)
        self.assertEqual(self.sent, [])

    def test_completed_lookup_uses_official_completed_filter(self):
        self.connected()
        self.respond("POST", "/task/completed", [self.task(status=2)])
        self.respond("POST", "/task/t1", b"")
        self.cli["set_task_status"]("t1", 0)
        self.assertEqual(json.loads(self.sent[0].data), {})

    def test_edit_preserves_details_and_checklist_with_official_wire_dates(self):
        task = self.task(items=[{"id": "i1", "title": "Step", "status": 0}], serverOnly="kept locally")
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("POST", "/task/t1", task)
        edited = self.cli["update_task"]("t1", {"title": "Edited", "tags": [], "due": "2026-10-27"})
        body = json.loads(self.sent[-1].data)
        self.assertEqual(body["title"], "Edited")
        self.assertEqual(body["content"], "Notes")
        self.assertEqual(body["items"], task["items"])
        self.assertEqual(body["tags"], [])
        self.assertNotIn("modifiedTime", body)
        self.assertNotIn("status", body)
        self.assertNotIn("serverOnly", body)
        self.assertNotIn(".000", body["dueDate"])
        self.assertEqual(edited["serverOnly"], "kept locally")

    def test_checklist_toggle_uses_documented_item_status(self):
        task = self.task(items=[{"id": "i1", "title": "Step", "status": 0}])
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("POST", "/task/t1", task)
        self.cli["toggle_subtask"]("t1", "i1")
        item = json.loads(self.sent[-1].data)["items"][0]
        self.assertEqual(item["status"], 1)
        self.assertIn("completedTime", item)

    def test_add_uses_singleton_route_and_returns_server_assigned_id(self):
        self.connected()
        self.cli["object_id"] = lambda: "local-task-reference"
        self.respond("POST", "/task", {"id": "server-task-id", "title": "Provider title", "projectId": "p1"})
        task = self.cli["create_task"]("New", "p1", "2026-10-27", tags=["WORK"])
        body = json.loads(self.sent[-1].data)
        self.assertEqual(task["id"], "server-task-id")
        self.assertEqual(task["title"], "Provider title")
        self.assertEqual(task["tags"], ["work"])
        self.assertEqual(body["title"], "New")
        self.assertEqual(body["projectId"], "p1")
        self.assertEqual(body["tags"], ["work"])
        self.assertNotIn(".000", body["dueDate"])
        self.assertNotIn("id", body)
        self.assertNotIn("status", body)
        self.assertNotIn("createdTime", body)
        self.assertEqual(self.cli["read_outbox"](), [])
        with self.assertRaisesRegex(self.cli["TickTickError"], "no inbox ID"):
            self.cli["create_task"]("New", None, None)
        self.assertEqual(len(self.sent), 1)

    def test_add_command_reports_authoritative_id(self):
        self.connected()
        self.respond("POST", "/task", self.task("server-task-id", title="New"))
        self.cli["sync_after_write"] = Mock()
        code, out, err = self.command(["add", "New", "--project", "p1", "--due", "today"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"ok": True, "id": "server-task-id", "title": "New"})
        self.cli["sync_after_write"].assert_called_once()

    def test_official_batch_add_returns_actual_singleton_tasks_without_id_map(self):
        tasks = [self.task("local-one"), self.task("local-two", title="Second")]
        self.respond("POST", "/task", self.task("server-one"))
        self.respond("POST", "/task", self.task("server-two", title="Second"))
        result = self.cli["batch_task"](self.session, add=tasks)
        self.assertEqual([task["id"] for task in result["created"]], ["server-one", "server-two"])
        self.assertEqual(result["updated"], [])
        self.assertNotIn("id2etag", result)
        self.assertEqual([task["id"] for task in tasks], ["local-one", "local-two"])
        self.assertTrue(all("id" not in json.loads(req.data) for req in self.sent))
        self.assertEqual(self.responses, [])

    def test_official_batch_partial_success_retains_confirmed_ids_and_stops(self):
        self.respond("POST", "/task", self.task("server-one"))
        self.respond("POST", "/task", b"", 503)
        with self.assertRaises(self.cli["TickTickError"]) as caught:
            self.cli["batch_task"](self.session, add=[self.task("local-one"), self.task("local-two"),
                                                      self.task("local-three")])
        self.assertEqual([task["id"] for task in caught.exception.created_tasks], ["server-one"])
        self.assertTrue(caught.exception.needs_review)
        self.assertFalse(caught.exception.transient)
        self.assertEqual(len(self.sent), 2)

    def test_ambiguous_task_create_timeout_is_retained_without_success_or_replay(self):
        self.connected()
        self.cli["object_id"] = lambda: "local-task-reference"
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as attempted:
            with self.assertRaisesRegex(self.cli["TickTickError"], "could not be confirmed") as caught:
                self.cli["create_task"]("New", "p1", "today")
            self.assertTrue(caught.exception.needs_review)
            self.assertFalse(caught.exception.transient)
            attempted.assert_called_once()
            req = attempted.call_args.args[0]
            self.assertEqual((req.method, req.full_url), ("POST", "https://api.ticktick.com/open/v1/task"))
            self.assertNotIn("id", json.loads(req.data))
            entry = self.cli["read_outbox"]()[0]
            self.assertEqual((entry["kind"], entry["backend"]), ("add", "openapi"))
            self.assertEqual(entry["owner"], self.cli["session_owner"](self.session))
            self.assertTrue(entry["payload"]["needsReview"])
            self.assertEqual(entry["payload"]["task"]["title"], "New")
            self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"], [])
            # The lost response may follow a committed task with a different
            # id. A later sync must neither overlay a phantom nor send again.
            self.cli["write_cache"](self.cache([self.task("server-task-id", title="New")]))
            for _ in range(2):
                self.assertEqual(self.cli["drain_outbox"](self.session), {"sent": 0, "queued": 1, "dropped": 0})
            attempted.assert_called_once()
        self.assertEqual([t["id"] for t in self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"]], ["server-task-id"])
        self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])

    def test_ambiguous_add_command_reports_failure_with_retained_review_entry(self):
        self.connected()
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as attempted:
            code, out, err = self.command(["add", "New", "--project", "p1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("saved in the outbox for review", err)
        attempted.assert_called_once()
        self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])

    def test_unusable_create_responses_and_server_errors_require_review(self):
        for body, status in (({}, 200), ({"id": ""}, 200), ({"id": 42}, 200), ([], 200),
                             (b"not-json", 200), (b"\xff", 200), (b"", 503)):
            with self.subTest(body=body, status=status):
                self.connected()
                self.cli["write_outbox"]([])
                self.respond("POST", "/task", body, status)
                with self.assertRaises(self.cli["TickTickError"]) as caught:
                    self.cli["create_task"]("New", "p1", None)
                self.assertTrue(caught.exception.needs_review)
                self.assertFalse(caught.exception.transient)
                self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])
                count = len(self.sent)
                self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
                self.assertEqual(len(self.sent), count)
                self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"], [])

    def test_definitive_create_rejection_leaves_no_pending_task(self):
        self.connected()
        for status in (400, 401, 403):
            self.respond("POST", "/task", b"", status)
            with self.assertRaises(self.cli["TickTickError"]) as caught:
                self.cli["create_task"]("New", "p1", None)
            self.assertEqual(caught.exception.status, status)
            self.assertFalse(caught.exception.needs_review)
            self.assertEqual(self.cli["read_outbox"](), [])
            self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"], [])

    def test_legacy_create_keeps_client_id_route_and_offline_replay(self):
        legacy = {"token": "dummy-legacy-cookie"}
        self.write("TOKEN_PATH", legacy)
        self.write("DATA_PATH", {"tasks": [], "inboxId": "legacy-inbox"})
        self.cli["object_id"] = lambda: "legacy-task-id"
        self.cli["request"] = Mock(return_value={"id2etag": {"legacy-task-id": "etag"}})
        task = self.cli["create_task"]("Legacy", None, None)
        self.assertEqual((task["id"], task["projectId"]), ("legacy-task-id", "legacy-inbox"))
        args, kwargs = self.cli["request"].call_args
        self.assertEqual(args, ("POST", "/batch/task", legacy))
        self.assertEqual(kwargs["payload"], {"add": [task], "update": [], "delete": []})
        self.cli["request"].side_effect = self.cli["TickTickError"]("Synthetic timeout", transient=True)
        queued = self.cli["create_task"]("Offline legacy", None, None)
        self.assertTrue(queued["queued"])
        self.assertFalse(self.cli["read_outbox"]()[0]["payload"].get("needsReview"))
        self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"][0]["id"], "legacy-task-id")
        self.cli["request"].side_effect = None
        self.assertEqual(self.cli["drain_outbox"](legacy), {"sent": 1, "queued": 0, "dropped": 0})
        self.assertEqual(self.cli["request"].call_args.kwargs["payload"]["add"][0]["id"], "legacy-task-id")

    def test_habit_checkin_uses_stamp_and_no_guessed_provider_status(self):
        self.connected()
        self.respond("GET", "/habit/checkins", [{"habitId": "h1", "checkins": [
            {"stamp": 20261025, "value": 1, "goal": 3, "status": 919}]}])
        self.respond("POST", "/habit/h1/checkin", {"habitId": "h1", "checkins": []})
        entry = self.cli["check_in"]({"id": "h1", "goal": 3, "step": 1}, 20261025, None, True)
        body = json.loads(self.sent[-1].data)
        self.assertEqual(body["stamp"], 20261025)
        self.assertEqual(body["value"], 2)
        self.assertNotIn("status", body)
        self.assertNotIn("checkinStamp", body)
        self.assertNotIn("habitId", body)
        self.assertEqual(entry["value"], 2)

    def test_offline_habit_checkin_queues_cached_increment_and_replays_official_body(self):
        self.connected(checkins={"h1": [{"habitId": "h1", "checkinStamp": 20261025,
                                          "value": 1, "goal": 3, "status": 0}]})
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError):
            entry = self.cli["check_in"]({"id": "h1", "goal": 3, "step": 1}, 20261025, None, True)
        self.assertTrue(entry["queued"])
        self.assertEqual(entry["value"], 2)
        self.respond("POST", "/habit/h1/checkin", {})
        self.assertEqual(self.cli["drain_outbox"](self.session)["sent"], 1)
        body = json.loads(self.sent[-1].data)
        self.assertEqual((body["stamp"], body["value"]), (20261025, 2))
        self.assertNotIn("status", body)

    def test_offline_edit_is_bound_and_replay_reads_fresh_record(self):
        self.connected([self.task()])
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError):
            result = self.cli["update_task"]("t1", {"title": "Queued"})
        self.assertTrue(result["queued"])
        entry = self.cli["read_outbox"]()[0]
        self.assertEqual(entry["owner"], self.cli["session_owner"](self.session))
        self.assertEqual(entry["backend"], "openapi")
        self.respond("GET", "/project/p1/task/t1", self.task(content="Changed elsewhere"))
        self.respond("POST", "/task/t1", {})
        self.assertEqual(self.cli["drain_outbox"](self.session)["sent"], 1)
        body = json.loads(self.sent[-1].data)
        self.assertEqual(body["content"], "Changed elsewhere")
        self.assertEqual(body["title"], "Queued")

    def test_official_checkbox_timeout_replays_desired_state_without_flipping_twice(self):
        task = self.task(items=[{"id": "i1", "title": "Step", "status": 0}])
        self.connected([task])
        self.respond("GET", "/project/p1/task/t1", task)
        original = self.cli["request"]

        def request(method, path, session, **kwargs):
            if method == "POST":
                raise self.cli["TickTickError"]("Synthetic timeout", transient=True)
            return original(method, path, session, **kwargs)

        self.cli["request"] = request
        result = self.cli["toggle_subtask"]("t1", "i1")
        self.assertTrue(result["queued"])
        self.assertEqual(self.cli["read_outbox"]()[0]["payload"]["item"]["status"], 1)
        self.cli["request"] = original
        # The timed-out update may already have succeeded remotely.
        task["items"][0]["status"] = 1
        self.respond("GET", "/project/p1/task/t1", task)
        self.respond("POST", "/task/t1", {})
        self.assertEqual(self.cli["drain_outbox"](self.session)["sent"], 1)
        self.assertEqual(json.loads(self.sent[-1].data)["items"][0]["status"], 1)

    def test_old_official_add_entry_is_retained_for_review_without_any_request(self):
        task = self.task()
        self.connected([task])
        self.cli["queue_write"]("add", {"task": task}, "offline", self.session)
        result = self.cli["drain_outbox"](self.session)
        self.assertEqual((result["sent"], result["queued"], result["dropped"]), (0, 1, 0))
        self.assertEqual(len(self.cli["_read_json"](self.cli["DATA_PATH"])["tasks"]), 1)
        self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])
        self.assertEqual(self.sent, [])

    def test_rejected_official_replay_retains_outbox(self):
        self.connected([self.task()])
        self.cli["queue_write"]("update", {"taskId": "t1", "fields": {"title": "Queued"}}, "offline", self.session)
        self.respond("GET", "/project/p1/task/t1", b"", 403)
        result = self.cli["drain_outbox"](self.session)
        self.assertEqual((result["dropped"], result["queued"]), (0, 1))

    def test_focus_creation_omits_undocumented_status_duration_and_id(self):
        self.connected()
        self.respond("POST", "/focus", {"id": "provider-focus-id", "type": 0})
        start = datetime(2026, 10, 25, 9, tzinfo=timezone.utc)
        end = datetime(2026, 10, 25, 9, 25, tzinfo=timezone.utc)
        result = self.cli["log_pomodoro"](start, end, 90, 0)
        self.assertEqual(result["id"], "provider-focus-id")
        self.assertEqual(json.loads(self.sent[-1].data), {
            "type": 0, "startTime": "2026-10-25T09:00:00+0000",
            "endTime": "2026-10-25T09:25:00+0000", "pauseDuration": 90})

    def test_ambiguous_focus_upload_is_retained_without_false_success_or_retry(self):
        self.connected(pomoStats={"todayPomoCount": 2})
        now = datetime.now(timezone.utc)
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as attempted:
            with self.assertRaisesRegex(self.cli["TickTickError"], "could not be confirmed"):
                self.cli["log_pomodoro"](now, now, 0, 0)
            for _ in range(2):
                self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
            attempted.assert_called_once()
        self.assertEqual(self.cli["_read_json"](self.cli["DATA_PATH"])["pomoStats"]["todayPomoCount"], 2)
        self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])
        self.assertEqual(self.sent, [])

    def test_focus_server_error_is_retained_for_review_without_replay(self):
        self.connected()
        self.respond("POST", "/focus", b"", 503)
        now = datetime.now(timezone.utc)
        with self.assertRaises(self.cli["TickTickError"]) as caught:
            self.cli["log_pomodoro"](now, now, 0, 0)
        self.assertTrue(caught.exception.needs_review)
        self.assertTrue(self.cli["read_outbox"]()[0]["payload"]["needsReview"])
        self.assertEqual(self.cli["drain_outbox"](self.session)["queued"], 1)
        self.assertEqual(len(self.sent), 1)

    def oauth_module(self, authorize):
        module = types.ModuleType("ticktick_oauth")

        class OAuthError(Exception):
            def __init__(self, code, message):
                self.code = code
                super().__init__(message)

        module.authorize = authorize
        module.OAuthError = OAuthError
        return module

    def test_oauth_streams_safe_events_and_stores_official_session(self):
        flushed = []

        def authorize(on_event, timeout=180):
            self.assertEqual(timeout, 180)
            for event in ({"phase": "preparing"}, {"phase": "waiting", "url": "https://ticktick.com/oauth/authorize?client_id=dummy-client"},
                          {"phase": "validating"}):
                on_event(event)
            self.assertFalse((self.state / "session.json").exists())
            return {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600, "client_id": "dummy-client"}

        class Output(io.StringIO):
            def flush(self):
                flushed.append(self.getvalue())

        out, err = Output(), io.StringIO()
        self.full_state()
        with patch.dict(sys.modules, {"ticktick_oauth": self.oauth_module(authorize)}), patch("subprocess.Popen") as spawn:
            with redirect_stdout(out), redirect_stderr(err):
                code = self.cli["main"](["login", "--oauth", "--json-events"])
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["phase"] for line in out.getvalue().splitlines()],
                         ["preparing", "waiting", "validating", "connected"])
        self.assertEqual(len(flushed), 4)
        spawn.assert_not_called()
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertEqual(self.cli["load_session"]()["source"], "oauth")
        self.assertIn("expiresAt", self.cli["load_session"]())

    def test_oauth_errors_leave_no_credentials_and_emit_ndjson(self):
        module = self.oauth_module(None)

        def authorize(on_event, timeout=180):
            on_event({"phase": "preparing"})
            raise module.OAuthError("authorization_denied", "Browser authorization was declined")

        module.authorize = authorize
        with patch.dict(sys.modules, {"ticktick_oauth": module}):
            code, out, err = self.command(["login", "--oauth", "--json-events"])
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(json.loads(out.splitlines()[-1])["code"], "authorization_denied")
        self.assertFalse((self.state / "session.json").exists())

    def test_oauth_pending_work_blocks_authorize_without_deleting_anything(self):
        self.connected([self.task()])
        self.cli["queue_write"]("update", {"taskId": "t1", "fields": {"title": "Pending"}}, "offline", self.session)
        module = self.oauth_module(lambda *a, **k: self.fail("Authorization must not start"))
        with patch.dict(sys.modules, {"ticktick_oauth": module}):
            code, out, _ = self.command(["login", "--oauth", "--json-events"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["phase"], "error")
        self.assertEqual(len(self.cli["read_outbox"]()), 1)
        self.assertEqual(self.cli["load_session"](), self.session)


if __name__ == "__main__":
    unittest.main()
