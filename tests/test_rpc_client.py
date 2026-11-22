import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from rpc_client import Backend, MAX_ABANDONED_REQUESTS, MAX_STDERR_CHARS, RpcError, RpcTimeoutError


FIXTURE = Path(__file__).parent / "fixtures/rpc_backend.py"


class RpcClientTests(unittest.TestCase):
    def backend(self, scenario="normal"):
        backend = Backend(command=[sys.executable, str(FIXTURE), scenario], request_timeout=2)
        self.addCleanup(backend.close)
        return backend

    def test_concurrent_responses_remain_correlated(self):
        b = self.backend()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda n: b.call("echo", {"n": n}), range(30)))
        self.assertEqual([x["echo"]["n"] for x in values], list(range(30)))
        self.assertEqual(b.diagnostics()["pending_count"], 0)

    def test_late_thread_creation_is_recoverable_without_repeat(self):
        b = self.backend()
        with self.assertRaises(RpcTimeoutError) as ctx:
            b.call("late", {}, timeout=0.005)
        self.assertEqual(ctx.exception.method, "late")
        deadline = time.monotonic() + 2
        while b.diagnostics()["late_response_count"] == 0 and time.monotonic() < deadline:
            seq = b.event_sequence
            b.wait_for_update(seq, 0.1)
        result = b.drain_late_responses()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["rpc_id"], ctx.exception.rpc_id)
        self.assertEqual(result[0]["result"]["thread"]["id"], "late-created-thread")
        self.assertEqual(b.drain_late_responses(), [])
        self.assertEqual(b.diagnostics()["abandoned_count"], 0)

    def test_abandoned_requests_are_bounded(self):
        b = self.backend()
        for _ in range(MAX_ABANDONED_REQUESTS + 7):
            with self.assertRaises(RpcTimeoutError):
                b.call("never", timeout=0.001)
        self.assertEqual(b.diagnostics()["abandoned_count"], MAX_ABANDONED_REQUESTS)

    def test_eof_fails_future_requests_immediately(self):
        b = self.backend()
        with self.assertRaises(RpcError):
            b.call("die")
        start = time.monotonic()
        with self.assertRaises(RpcError):
            b.call("echo", timeout=5)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_invalid_protocol_fails_closed_without_raw_text(self):
        for method in ("invalid", "array"):
            with self.subTest(method=method):
                b = self.backend()
                with self.assertRaises(RpcError) as ctx:
                    b.call(method)
                self.assertIn("Invalid JSONL", str(ctx.exception))
                self.assertNotIn("private", str(ctx.exception))
                self.assertTrue(b.diagnostics()["closed"])

    def test_initialize_failure_reaps_process(self):
        real_popen = subprocess.Popen
        spawned = []

        def launch(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        with mock.patch("rpc_client.subprocess.Popen", side_effect=launch):
            with self.assertRaises(RpcError):
                Backend(command=[sys.executable, str(FIXTURE), "bad-init"])
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())

    def test_stderr_is_bounded_and_never_returned_as_diagnostics(self):
        b = self.backend()
        b.call("stderr")
        deadline = time.monotonic() + 2
        while b.diagnostics()["stderr_chars_seen"] < 100_000 and time.monotonic() < deadline:
            time.sleep(0.01)
        report = b.diagnostics()
        self.assertGreaterEqual(report["stderr_chars_seen"], 100_000)
        self.assertLessEqual(report["stderr_chars_retained"], MAX_STDERR_CHARS)
        self.assertNotIn("secret_token", json.dumps(report))

    def test_server_approval_is_rejected_and_correlated(self):
        b = self.backend()
        response = b.call("approval")
        self.assertIn("error", response["reply"])
        self.assertNotIn("result", response["reply"])
        self.assertEqual(response["reply"]["id"], 900)
        self.assertIn("test-thread", b.attention)

    def test_token_notifications_update_usage(self):
        b = self.backend()
        before = b.event_sequence
        b.call("usage")
        self.assertGreater(b.event_sequence, before)
        self.assertEqual(b.usage["test-thread"]["total"]["totalTokens"], 123)

    def test_a_new_turn_clears_stale_attention(self):
        b = self.backend()
        b.call("approval")
        self.assertIn("test-thread", b.attention)
        b.call("new-turn")
        self.assertNotIn("test-thread", b.attention)

    def test_close_unblocks_pending_call_and_reaps_process(self):
        b = self.backend()
        outcome = []

        def wait_forever():
            try:
                b.call("never", timeout=20)
            except RpcError as exc:
                outcome.append(str(exc))

        waiter = threading.Thread(target=wait_forever)
        waiter.start()
        deadline = time.monotonic() + 2
        while not b.diagnostics()["pending_count"] and time.monotonic() < deadline:
            time.sleep(0.005)
        b.close()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(outcome)
        self.assertIsNotNone(b.proc.poll())

    def test_stubborn_child_is_killed_with_bounded_shutdown(self):
        b = self.backend("stubborn")
        start = time.monotonic()
        b.close()
        self.assertLess(time.monotonic() - start, 4)
        self.assertIsNotNone(b.proc.poll())


if __name__ == "__main__":
    unittest.main()
