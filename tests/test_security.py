#!/usr/bin/env python3
"""Credential-boundary regressions: synthetic secrets, no external networking."""

import io
import json
import os
from contextlib import redirect_stdout, redirect_stderr
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
import runpy
import ssl
import unittest
from email.message import Message
from unittest.mock import patch
import urllib.request
import urllib.response


ROOT = Path(__file__).resolve().parents[1]


def load_cli():
    return runpy.run_path(str(ROOT / "bin" / "omarchy-ticktick"))["request"].__globals__


class RequestSecurityTests(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()
        self.sent = []
        self.responses = []
        self.contexts = []
        self.sizes = []
        owner = self

        class Body(io.BytesIO):
            def read(self, size: int | None = -1):
                owner.sizes.append(size)
                return super().read(size)

        def transport(handler, req):
            self.sent.append(req)
            self.contexts.append(getattr(handler, "_context", None))
            status, headers, body = self.responses.pop(0)
            message = Message()
            for key, value in headers.items():
                message[key] = value
            response = urllib.response.addinfourl(Body(body), message, req.full_url, status)
            setattr(response, "msg", "Test response")
            return response

        for target in ("HTTPSHandler.https_open", "HTTPHandler.http_open"):
            p = patch("urllib.request." + target, transport)
            p.start()
            self.addCleanup(p.stop)
        p = patch("urllib.request.getproxies", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def test_redirects_never_receive_cookie_or_password(self):
        for destination in (
            "https://different.invalid/collect",
            "https://api.ticktick.com:444/collect",
            "http://api.ticktick.com/collect",
            "https://api.ticktick.com/same-origin",
        ):
            for code in (301, 302, 303, 307, 308):
                for method, payload in (("GET", None), ("POST", {"password": "dummy-password"})):
                    with self.subTest(destination=destination, code=code, method=method):
                        self.sent.clear()
                        self.responses = [(code, {"Location": destination}, b""), (200, {}, b"{}")]
                        with self.assertRaises(self.cli["TickTickError"]) as raised:
                            self.cli["request"](method, "/test", {"token": "dummy-cookie"}, payload=payload)
                        self.assertFalse(raised.exception.transient)
                        self.assertEqual(len(self.sent), 1)
                        self.assertEqual(self.sent[0].get_header("Cookie"), "t=dummy-cookie")
                        self.assertNotIn(destination, str(raised.exception))

    def test_initial_http_is_rejected_before_transport(self):
        self.cli["API"] = "http://api.ticktick.com/api/v2"
        self.responses = [(200, {}, b"{}")]
        with self.assertRaises(self.cli["TickTickError"]):
            self.cli["request"]("GET", "/test", {"token": "dummy-cookie"})
        self.assertEqual(self.sent, [])

    def test_normal_https_retains_verification_timeout_and_body_limit(self):
        self.responses = [(200, {}, b'{"ok":true}')]
        result = self.cli["request"]("GET", "/test", {"token": "dummy-cookie"}, timeout=7)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.sent[0].timeout, 7)
        self.assertTrue(self.contexts[0].check_hostname)
        self.assertEqual(self.contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(self.sizes, [self.cli["MAX_BODY_BYTES"] + 1])

    def test_large_response_is_rejected(self):
        self.cli["MAX_BODY_BYTES"] = 8
        self.responses = [(200, {}, b" " * 9)]
        with self.assertRaisesRegex(self.cli["TickTickError"], "exceeded"):
            self.cli["request"]("GET", "/test", {})

    def test_error_response_read_is_bounded(self):
        self.responses = [(500, {}, b"failure")]
        with self.assertRaises(self.cli["TickTickError"]):
            self.cli["request"]("GET", "/test", {})
        self.assertEqual(self.sizes, [self.cli["MAX_ERROR_BODY_BYTES"]])

    def test_untrusted_error_bodies_cannot_echo_credentials_into_logs(self):
        for status, body in (
            (500, b"dummy-cookie dummy-password"),
            (200, b'{"errorCode":"unknown","errorMessage":"dummy-cookie dummy-password"}'),
        ):
            with self.subTest(status=status):
                self.responses = [(status, {}, body)]
                with self.assertRaises(self.cli["TickTickError"]) as raised:
                    self.cli["request"]("POST", "/test", {"token": "dummy-cookie"},
                                        payload={"password": "dummy-password"})
                self.assertNotIn("dummy-cookie", str(raised.exception))
                self.assertNotIn("dummy-password", str(raised.exception))

    def test_known_auth_errors_still_request_reauthentication(self):
        self.responses = [(401, {}, b'{"errorCode":"token_expired"}')]
        with self.assertRaises(self.cli["TickTickError"]) as raised:
            self.cli["request"]("GET", "/test", {})
        self.assertTrue(raised.exception.auth_required)


@contextmanager
def umask(value):
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state" / "omarchy" / "ticktick"
        self.cli["STATE_DIR"] = str(self.state)
        for name, filename in (("TOKEN_PATH", "session.json"), ("DATA_PATH", "data.json"),
                               ("OUTBOX_PATH", "outbox.json"), ("DRAIN_LOCK_PATH", "drain.lock")):
            self.cli[name] = str(self.state / filename)


class StorageSecurityTests(StateFixture):
    def test_fresh_directory_and_files_are_private(self):
        with umask(0):
            self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "dummy-cookie"})
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(self.state.stat().st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(Path(self.cli["TOKEN_PATH"]).stat().st_mode), 0o600)

    def test_existing_directory_is_tightened_before_writing(self):
        self.state.mkdir(parents=True)
        self.state.chmod(0o777)
        original = json.dump

        def observe(payload, handle, **kwargs):
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
            return original(payload, handle, **kwargs)

        with patch("json.dump", observe):
            self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "dummy-cookie"})

    def test_temporary_file_is_private_before_first_byte(self):
        original = json.dump

        def observe(payload, handle, **kwargs):
            self.assertEqual(stat.S_IMODE(os.fstat(handle.fileno()).st_mode), 0o600)
            self.assertEqual(os.fstat(handle.fileno()).st_size, 0)
            return original(payload, handle, **kwargs)

        with umask(0), patch("json.dump", observe):
            self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "dummy-cookie"})

    def test_symlinked_directory_or_ancestor_is_refused(self):
        for leaf in (True, False):
            with self.subTest(leaf=leaf):
                target = self.root / ("target-leaf" if leaf else "target-parent")
                target.mkdir()
                target.chmod(0o755)
                if leaf:
                    self.state.parent.mkdir(parents=True)
                    link = self.state
                else:
                    link = self.root / "linked-state"
                    self.cli["STATE_DIR"] = str(link / "omarchy" / "ticktick")
                    self.cli["TOKEN_PATH"] = str(link / "omarchy" / "ticktick" / "session.json")
                link.symlink_to(target, target_is_directory=True)
                with self.assertRaises(self.cli["TickTickError"]):
                    self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "dummy-cookie"})
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_foreign_owned_directory_is_refused_without_chmod(self):
        self.state.mkdir(parents=True)
        self.state.chmod(0o755)
        with patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(self.cli["TickTickError"]):
                self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "dummy-cookie"})
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)

    def test_predictable_old_temp_symlink_cannot_clobber_another_file(self):
        self.state.mkdir(parents=True)
        victim = self.root / "victim"
        victim.write_text("unchanged")
        old_tmp = Path(self.cli["TOKEN_PATH"] + f".{os.getpid()}.tmp")
        old_tmp.symlink_to(victim)
        self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "dummy-cookie"})
        self.assertEqual(victim.read_text(), "unchanged")

    def test_failed_write_keeps_old_session_and_removes_private_temp(self):
        self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "old-dummy-cookie"})
        with patch("json.dump", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "new-dummy-cookie"})
        self.assertEqual(json.loads(Path(self.cli["TOKEN_PATH"]).read_text()), {"token": "old-dummy-cookie"})
        self.assertEqual([p.name for p in self.state.iterdir()], ["session.json"])


class TokenHandoffTests(StateFixture):

    def handoff(self, content="dummy-cookie", mode=0o600):
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / "token-paste"
        path.write_text(content)
        path.chmod(mode)
        return path

    def test_private_legacy_token_file_is_consumed_and_removed(self):
        path = self.handoff()
        self.state.chmod(0o755)
        self.assertEqual(self.cli["consume_token_file"](str(path)), "dummy-cookie")
        self.assertFalse(path.exists())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_unsafe_handoff_is_rejected_and_removed(self):
        for kind in ("symlink", "public", "oversized"):
            with self.subTest(kind=kind):
                path = self.handoff()
                if kind == "symlink":
                    target = self.root / "target"
                    target.write_text("dummy-cookie")
                    path.unlink()
                    path.symlink_to(target)
                elif kind == "public":
                    path.chmod(0o644)
                elif kind == "oversized":
                    path.write_text("x" * 4097)
                with self.assertRaises(self.cli["TickTickError"]):
                    self.cli["consume_token_file"](str(path), wait_seconds=0.01)
                self.assertFalse(path.is_symlink())
                self.assertFalse(path.exists())
                if kind == "symlink":
                    self.assertEqual(target.read_text(), "dummy-cookie")

    def test_directory_handoff_is_rejected_without_recursive_cleanup(self):
        path = self.state / "token-paste"
        path.mkdir(parents=True)
        with self.assertRaises(self.cli["TickTickError"]):
            self.cli["consume_token_file"](str(path), wait_seconds=0.01)
        self.assertTrue(path.is_dir())

    def test_missing_handoff_does_not_create_a_credential(self):
        path = self.state / "token-paste"
        with self.assertRaises(self.cli["TickTickError"]):
            self.cli["consume_token_file"](str(path), wait_seconds=0.01)
        self.assertFalse(path.exists())

    def test_failed_login_removes_handoff_and_keeps_previous_session(self):
        self.cli["_write_private"](self.cli["TOKEN_PATH"], {"token": "previous-dummy"})
        path = self.handoff()
        self.cli["fetch_state"] = lambda _: (_ for _ in ()).throw(self.cli["TickTickError"]("Rejected"))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli["main"](["login", "--token-file", str(path)])
        self.assertEqual(code, 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.cli["load_session"](), {"token": "previous-dummy"})
        self.assertNotIn("dummy", out.getvalue() + err.getvalue())

    def test_stdin_login_uses_same_validation_and_private_session_writer(self):
        self.cli["fetch_state"] = lambda session: {"tasks": [], "habits": []}
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO("t=dummy-cookie\n")), redirect_stdout(out), redirect_stderr(err):
            code = self.cli["main"](["login", "--token-stdin"])
        self.assertEqual(code, 0)
        self.assertEqual(self.cli["load_session"]()["token"], "dummy-cookie")
        self.assertEqual(stat.S_IMODE(Path(self.cli["TOKEN_PATH"]).stat().st_mode), 0o600)
        self.assertFalse((self.state / "token-paste").exists())
        self.assertNotIn("dummy-cookie", out.getvalue() + err.getvalue())

    def test_malformed_token_is_rejected_without_logging_or_network(self):
        calls = []
        self.cli["fetch_state"] = lambda session: calls.append(session) or {"tasks": [], "habits": []}
        for token in ("dummy-cookie\r\nInjected: yes", "dummy-cookie\x00", "dummy-cookie\u0100", "x" * 4097):
            with self.subTest(kind="malformed token"):
                with self.assertRaises(self.cli["TickTickError"]) as raised:
                    self.cli["connect_with_token"](token, {})
                self.assertNotIn("dummy-cookie", str(raised.exception))
        self.assertEqual(calls, [])

    def test_failed_stdin_login_leaves_no_token_or_handoff(self):
        self.cli["fetch_state"] = lambda _: (_ for _ in ()).throw(self.cli["TickTickError"]("Rejected"))
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO("dummy-cookie\n")), redirect_stdout(out), redirect_stderr(err):
            code = self.cli["main"](["login", "--token-stdin"])
        self.assertEqual(code, 1)
        self.assertFalse(Path(self.cli["TOKEN_PATH"]).exists())
        self.assertFalse((self.state / "token-paste").exists())
        self.assertNotIn("dummy-cookie", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
