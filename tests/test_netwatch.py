"""Tests for netwatch probing, storage, and the HTTP API."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import netwatch
from netwatch import Store, load_targets, make_handler, probe
from tests.support.fakes import closed_port, tcp_listener


class TestProbe(unittest.TestCase):
    def test_open_port_reports_up_with_latency(self):
        with tcp_listener() as (host, port):
            up, latency, error = probe(host, port, timeout=2.0)
        self.assertTrue(up)
        self.assertIsNone(error)
        self.assertIsInstance(latency, float)
        self.assertGreaterEqual(latency, 0.0)

    def test_closed_port_reports_down_with_reason(self):
        host, port = closed_port()
        up, latency, error = probe(host, port, timeout=2.0)
        self.assertFalse(up)
        self.assertIsNone(latency)
        self.assertTrue(error)

    def test_unresolvable_host_is_reported_as_dns_failure(self):
        up, _, error = probe("no-such-host.invalid", 80, timeout=2.0)
        self.assertFalse(up)
        self.assertIn("dns", error.lower())


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._tmp.name, "nw.db"))

    def tearDown(self):
        self._tmp.cleanup()


class TestStore(StoreTestCase):
    def test_recorded_check_comes_back(self):
        self.store.record("api", "10.0.0.1", 443, True, 12.5, None)
        rows = self.store.latest()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "api")
        self.assertEqual(rows[0]["up"], 1)
        self.assertEqual(rows[0]["latency_ms"], 12.5)

    def test_latest_returns_the_most_recent_check_per_target(self):
        self.store.record("api", "10.0.0.1", 443, True, 10.0, None)
        self.store.record("api", "10.0.0.1", 443, False, None, "timed out")
        rows = self.store.latest()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["up"], 0)
        self.assertEqual(rows[0]["error"], "timed out")

    def test_several_targets_are_tracked_separately(self):
        self.store.record("api", "10.0.0.1", 443, True, 10.0, None)
        self.store.record("db", "10.0.0.2", 5432, True, 2.0, None)
        self.assertEqual({r["target"] for r in self.store.latest()},
                         {"api", "db"})

    def test_uptime_is_a_percentage_of_successful_checks(self):
        for up in (True, True, False, True):
            self.store.record("api", "10.0.0.1", 443, up,
                              1.0 if up else None, None)
        self.assertAlmostEqual(self.store.latest()[0]["uptime_24h"], 75.0)

    def test_average_latency_ignores_failed_checks(self):
        self.store.record("api", "10.0.0.1", 443, True, 10.0, None)
        self.store.record("api", "10.0.0.1", 443, False, None, "down")
        self.store.record("api", "10.0.0.1", 443, True, 20.0, None)
        self.assertAlmostEqual(self.store.latest()[0]["avg_latency"], 15.0)

    def test_history_is_oldest_first(self):
        for latency in (1.0, 2.0, 3.0):
            self.store.record("api", "10.0.0.1", 443, True, latency, None)
        points = self.store.history("api")
        self.assertEqual([p["latency_ms"] for p in points], [1.0, 2.0, 3.0])

    def test_history_respects_the_limit(self):
        for i in range(10):
            self.store.record("api", "10.0.0.1", 443, True, float(i), None)
        self.assertEqual(len(self.store.history("api", limit=3)), 3)

    def test_history_of_unknown_target_is_empty(self):
        self.assertEqual(self.store.history("ghost"), [])

    def test_empty_store_returns_no_rows(self):
        self.assertEqual(self.store.latest(), [])

    def test_prune_deletes_only_old_rows(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self.store._transaction() as conn:
            conn.execute(
                "INSERT INTO checks (target, host, port, up, latency_ms,"
                " error, checked_at) VALUES (?,?,?,?,?,?,?)",
                ("api", "10.0.0.1", 443, 1, 5.0, None, old))
        self.store.record("api", "10.0.0.1", 443, True, 5.0, None)
        self.assertEqual(self.store.prune(keep_days=7), 1)
        self.assertEqual(len(self.store.history("api")), 1)

    def test_target_name_with_quotes_is_stored_as_data(self):
        """Parameterised SQL: a crafted name cannot alter the query."""
        name = "api'; DROP TABLE checks; --"
        self.store.record(name, "10.0.0.1", 443, True, 1.0, None)
        self.assertEqual(self.store.latest()[0]["target"], name)


class TestConfigLoading(unittest.TestCase):
    def write_config(self, payload) -> str:
        path = os.path.join(self._tmp.name, "targets.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_config_loads(self):
        path = self.write_config([{"name": "api", "host": "h", "port": 443}])
        self.assertEqual(load_targets(path),
                         [{"name": "api", "host": "h", "port": 443}])

    def test_port_is_coerced_to_int(self):
        path = self.write_config([{"name": "api", "host": "h", "port": "443"}])
        self.assertEqual(load_targets(path)[0]["port"], 443)

    def test_missing_field_is_rejected(self):
        path = self.write_config([{"name": "api", "host": "h"}])
        with self.assertRaises(ValueError):
            load_targets(path)

    def test_empty_config_is_rejected(self):
        with self.assertRaises(ValueError):
            load_targets(self.write_config([]))


class TestHttpApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._tmp.name, "nw.db"))
        self.store.record("api", "10.0.0.1", 443, True, 12.0, None)
        self.store.record("db", "10.0.0.2", 5432, False, None, "refused")
        targets = [{"name": "api", "host": "10.0.0.1", "port": 443}]
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.store, targets, 30.0))
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       args=(0.01,), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def fetch(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as resp:
                return resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            headers = exc.headers
            exc.close()
            return exc.code, headers, body

    def test_status_endpoint_lists_targets(self):
        status, _, body = self.fetch("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual({t["target"] for t in data["targets"]}, {"api", "db"})
        self.assertEqual(data["configured"], 1)

    def test_history_endpoint_returns_points(self):
        status, _, body = self.fetch("/api/history?target=api")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["points"]), 1)

    def test_history_without_target_is_a_400(self):
        status, _, _ = self.fetch("/api/history")
        self.assertEqual(status, 400)

    def test_healthz_reports_ok(self):
        status, _, body = self.fetch("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_dashboard_is_served_at_root(self):
        status, headers, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("netwatch", body)

    def test_static_assets_are_served(self):
        for path, expected in (("/style.css", "text/css"),
                               ("/app.js", "javascript")):
            status, headers, _ = self.fetch(path)
            self.assertEqual(status, 200)
            self.assertIn(expected, headers["Content-Type"])

    def test_path_traversal_is_blocked(self):
        status, _, body = self.fetch("/../netwatch.py")
        self.assertIn(status, (403, 404))
        self.assertNotIn("import sqlite3", body)  # no source code leaked

    def test_escaping_static_root_is_forbidden(self):
        status, _, _ = self.fetch("/..%2fnetwatch.py")
        self.assertIn(status, (403, 404))

    def test_security_headers_are_sent(self):
        _, headers, _ = self.fetch("/api/status")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])


class TestPoller(unittest.TestCase):
    def test_one_round_records_every_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "nw.db"))
            with tcp_listener() as (host, port):
                closed_host, closed_p = closed_port()
                targets = [{"name": "up", "host": host, "port": port},
                           {"name": "down", "host": closed_host,
                            "port": closed_p}]
                poller = netwatch.Poller(targets, store, interval=60.0,
                                         timeout=2.0, keep_days=7)
                poller.start()
                # The first round runs immediately; wait for both rows.
                for _ in range(100):
                    if len(store.latest()) == 2:
                        break
                    threading.Event().wait(0.05)
                poller.stop()
                poller.join(timeout=3)
            rows = {r["target"]: r for r in store.latest()}
            self.assertEqual(rows["up"]["up"], 1)
            self.assertEqual(rows["down"]["up"], 0)


if __name__ == "__main__":
    unittest.main()


class TestPollerThreadHygiene(unittest.TestCase):
    """Regression guard for a bug the CI matrix caught on Python 3.10-3.12.

    `threading.Thread` has a private `_stop()` method in CPython <= 3.12.
    Storing an Event on `self._stop` shadowed it, and the thread raised
    "'Event' object is not callable" as it exited — invisible on 3.13+.
    """

    def build(self) -> tuple[netwatch.Poller, Store, tempfile.TemporaryDirectory]:
        tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(tmp.name, "nw.db"))
        host, port = closed_port()
        poller = netwatch.Poller([{"name": "t", "host": host, "port": port}],
                                 store, interval=0.05, timeout=0.5, keep_days=7)
        return poller, store, tmp

    def test_poller_does_not_shadow_thread_internals(self):
        poller, _, tmp = self.build()
        with tmp:
            # Any attribute Thread uses internally must not be an Event.
            for name in ("_stop", "_bootstrap", "_bootstrap_inner"):
                attr = getattr(poller, name, None)
                self.assertNotIsInstance(
                    attr, threading.Event,
                    f"Poller.{name} shadows a threading.Thread internal")

    def test_thread_exits_without_raising(self):
        poller, _, tmp = self.build()
        errors = []
        original = threading.excepthook
        threading.excepthook = lambda args: errors.append(args.exc_value)
        try:
            with tmp:
                poller.start()
                poller.stop()
                poller.join(timeout=5)
        finally:
            threading.excepthook = original
        self.assertFalse(poller.is_alive(), "poller thread did not terminate")
        self.assertEqual(errors, [], f"thread raised on exit: {errors}")

    def test_poller_runs_as_a_daemon(self):
        poller, _, tmp = self.build()
        with tmp:
            self.assertTrue(poller.daemon)
