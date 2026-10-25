"""A malformed error envelope must not bypass official write retention."""
import unittest

import test_openapi


class HttpErrorEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.f = test_openapi.OpenApiTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.connected()

    def test_deeply_malformed_server_error_still_retains_uncertain_creation(self):
        self.f.respond("POST", "/task", b"[" * 2000, status=500)
        with self.assertRaises(self.f.cli["TickTickError"]) as caught:
            self.f.cli["create_task"]("QA task", "p1", "today")
        self.assertTrue(caught.exception.needs_review)
        entries = self.f.cli["read_outbox"]()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["payload"]["needsReview"])
        sent = len(self.f.sent)
        self.f.cli["drain_outbox"](self.f.session)
        self.assertEqual(len(self.f.sent), sent)

    def test_deeply_malformed_auth_error_remains_a_definitive_rejection(self):
        self.f.respond("POST", "/task", b"[" * 2000, status=401)
        with self.assertRaises(self.f.cli["TickTickError"]) as caught:
            self.f.cli["create_task"]("QA task", "p1", "today")
        self.assertTrue(caught.exception.auth_required)
        self.assertEqual(self.f.cli["read_outbox"](), [])
