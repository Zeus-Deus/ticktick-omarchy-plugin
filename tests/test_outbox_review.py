"""Offline public recovery for explicitly review-only outbox entries."""

from contextlib import contextmanager
import json
import stat
import unittest
from unittest.mock import Mock, patch

import test_openapi


class OutboxReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = test_openapi.OpenApiTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.cli = self.f.cli
        self.f.connected([self.f.task(title="Cached task")])
        self.cli["request"] = Mock(side_effect=AssertionError("Outbox recovery must not use the API"))

    def entry(self, number, kind="add", review: bool | str | int | None = True):
        return {"id": f"{number:024x}", "kind": kind,
                "payload": {"task": {"title": f"Task {number}"}, "needsReview": review},
                "owner": self.cli["session_owner"](self.f.session), "backend": "openapi",
                "lastError": "Untrusted reply: " + test_openapi.TOKEN,
                "session": self.f.session, "token": "never-print-entry-token",
                "attempts": 3, "queuedAt": 123}

    def snapshot(self):
        return {key: self.f.state.joinpath(name).read_bytes() for key, name in
                (("token", "session.json"), ("cache", "data.json"), ("outbox", "outbox.json"))}

    def command(self, args):
        result = self.f.command(args)
        self.cli["request"].assert_not_called()
        self.assertEqual(self.f.sent, [])
        return result

    def test_inspection_is_allowlisted_sanitized_and_preserves_storage(self):
        reviewed = self.entry(1)
        reviewed["payload"]["task"]["title"] = "Task " + test_openapi.TOKEN + " " + reviewed["owner"]
        pending = self.entry(2, "complete", False)
        pending["payload"] = {"taskId": "t1"}
        self.cli["write_outbox"]([reviewed, pending])
        before = self.snapshot()
        code, out, err = self.command(["outbox"])
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)["entries"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["needsReview"])
        self.assertFalse(rows[1]["needsReview"])
        self.assertEqual(rows[1]["title"], "Cached task")
        self.assertEqual([row["id"] for row in rows], [reviewed["id"], pending["id"]])
        for row in rows:
            self.assertEqual(set(row), {"id", "kind", "title", "needsReview", "error"})
        for secret in (test_openapi.TOKEN, reviewed["owner"], "never-print-entry-token", "Untrusted reply"):
            self.assertNotIn(secret, out + err)
        self.assertEqual(self.snapshot(), before)

    def test_discard_without_confirmation_does_not_even_acquire_writer(self):
        self.cli["write_outbox"]([self.entry(1)])
        before = self.snapshot()
        with patch.dict(self.cli, {"account_lock": Mock(side_effect=AssertionError("No confirmed mutation"))}):
            code, out, err = self.command(["outbox", "discard-reviewed"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--confirm", err)
        self.assertEqual(self.snapshot(), before)

    def test_confirmed_discard_removes_only_true_review_flags_in_order(self):
        entries = [self.entry(1), self.entry(2, review=False), self.entry(3, "pomo"),
                   self.entry(4, review="true"), self.entry(5, review=1), self.entry(6, review=None)]
        self.cli["write_outbox"](entries)
        before = self.snapshot()
        code, out, err = self.command(["outbox", "discard-reviewed", "--confirm"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"ok": True, "discarded": 2, "remaining": 4})
        self.assertEqual(self.cli["read_outbox"](), [entries[i] for i in (1, 3, 4, 5)])
        self.assertEqual(self.snapshot()["token"], before["token"])
        self.assertEqual(self.snapshot()["cache"], before["cache"])
        self.assertEqual(stat.S_IMODE((self.f.state / "outbox.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.f.state.stat().st_mode), 0o700)

    def test_inspection_and_discard_use_the_real_account_lock(self):
        self.cli["write_outbox"]([self.entry(1)])
        held, entered = False, []
        original_lock = self.cli["account_lock"]
        original_read, original_write = self.cli["read_outbox"], self.cli["write_outbox"]

        @contextmanager
        def lock():
            nonlocal held
            with original_lock():
                held = True
                entered.append(True)
                try:
                    yield
                finally:
                    held = False

        def read():
            self.assertTrue(held)
            return original_read()

        def write(entries):
            self.assertTrue(held)
            return original_write(entries)

        with patch.dict(self.cli, {"account_lock": lock, "read_outbox": read, "write_outbox": write}):
            self.assertEqual(self.command(["outbox"])[0], 0)
            self.assertEqual(self.command(["outbox", "discard-reviewed", "--confirm"])[0], 0)
        self.assertEqual(len(entered), 2)

    def test_discarding_reviewed_entries_unblocks_logout_without_network(self):
        self.cli["write_outbox"]([self.entry(1)])
        self.assertEqual(self.command(["logout"])[0], 1)
        self.assertEqual(self.command(["outbox", "discard-reviewed", "--confirm"])[0], 0)
        # A different credential can now pass the local switch guard, too.
        self.cli["ensure_session_switch_safe"](dict(self.f.session, token="another-dummy-token"))
        self.assertEqual(self.command(["logout"])[0], 0)
        self.assertFalse((self.f.state / "session.json").exists())

    def test_ordinary_pending_writes_still_block_logout_after_discard(self):
        pending = self.entry(2, review=False)
        self.cli["write_outbox"]([self.entry(1), pending])
        self.assertEqual(self.command(["outbox", "discard-reviewed", "--confirm"])[0], 0)
        self.assertEqual(self.command(["logout"])[0], 1)
        self.assertEqual(self.cli["read_outbox"](), [pending])

    def test_empty_discard_is_noop_and_inspection_needs_no_credentials(self):
        self.cli["write_outbox"]([])
        (self.f.state / "session.json").unlink()
        before = (self.f.state / "outbox.json").read_bytes()
        code, out, err = self.command(["outbox"])
        self.assertEqual((code, err, json.loads(out)), (0, "", {"entries": []}))
        code, out, err = self.command(["outbox", "discard-reviewed", "--confirm"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"ok": True, "discarded": 0, "remaining": 0})
        self.assertEqual((self.f.state / "outbox.json").read_bytes(), before)
        self.assertFalse((self.f.state / "session.json").exists())

    def test_discard_failure_is_sanitized_and_preserves_old_entries(self):
        entries = [self.entry(1), self.entry(2, review=False)]
        self.cli["write_outbox"](entries)
        before = self.snapshot()
        with patch.dict(self.cli, {"write_outbox": Mock(side_effect=OSError(test_openapi.TOKEN))}):
            code, out, err = self.command(["outbox", "discard-reviewed", "--confirm"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Cannot discard", err)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
