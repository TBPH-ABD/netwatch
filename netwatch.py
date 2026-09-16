#!/usr/bin/env python3
"""netwatch — network service monitoring with a live web dashboard.

Polls a set of TCP services on an interval, records latency and availability
to SQLite, and serves a dashboard plus a JSON API from the standard library's
own HTTP server. No web framework, no database server, no build step.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_DB = "netwatch.db"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
class Store:
    """SQLite-backed check history. Safe to use from the poller and the server."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self):
        """Open a connection, commit or roll back, and always close it.

        `with sqlite3.connect(...)` manages the transaction but does *not*
        close the connection — over a long monitoring run that leaks a file
        descriptor on every poll.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    target     TEXT    NOT NULL,
                    host       TEXT    NOT NULL,
                    port       INTEGER NOT NULL,
                    up         INTEGER NOT NULL,
                    latency_ms REAL,
                    error      TEXT,
                    checked_at TEXT    NOT NULL
                )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_target_time "
                "ON checks(target, checked_at DESC)")

    def record(self, target: str, host: str, port: int, up: bool,
               latency_ms: float | None, error: str | None) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO checks (target, host, port, up, latency_ms, error,"
                " checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (target, host, port, int(up), latency_ms, error,
                 datetime.now(timezone.utc).isoformat()))

    def latest(self) -> list[dict]:
        """Most recent check for each target, with 24h uptime and averages."""
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._transaction() as conn:
            rows = conn.execute("""
                SELECT c.*,
                       (SELECT AVG(up) * 100 FROM checks
                         WHERE target = c.target AND checked_at >= ?)  AS uptime_24h,
                       (SELECT AVG(latency_ms) FROM checks
                         WHERE target = c.target AND checked_at >= ?
                           AND up = 1)                                 AS avg_latency,
                       (SELECT COUNT(*) FROM checks
                         WHERE target = c.target AND checked_at >= ?)  AS checks_24h
                  FROM checks c
                  JOIN (SELECT target, MAX(id) AS max_id FROM checks
                         GROUP BY target) m ON c.id = m.max_id
                 ORDER BY c.target
            """, (since, since, since)).fetchall()
        return [dict(row) for row in rows]

    def history(self, target: str, limit: int = 60) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT up, latency_ms, checked_at FROM checks WHERE target = ?"
                " ORDER BY id DESC LIMIT ?", (target, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def prune(self, keep_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._lock, self._transaction() as conn:
            cursor = conn.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))
            return cursor.rowcount


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------
def probe(host: str, port: int, timeout: float) -> tuple[bool, float | None, str | None]:
    """TCP connect probe. Returns (up, latency_ms, error)."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = (time.perf_counter() - started) * 1000
            return True, round(latency, 2), None
    except socket.timeout:
        return False, None, "connection timed out"
    except socket.gaierror as exc:
        return False, None, f"dns resolution failed: {exc.strerror or exc}"
    except OSError as exc:
        return False, None, exc.strerror or str(exc)


class Poller(threading.Thread):
    """Background thread that probes every configured target on an interval."""

    def __init__(self, targets: list[dict], store: Store, interval: float,
                 timeout: float, keep_days: int) -> None:
        # daemon is a property on Thread, so it is set through the constructor
        # rather than shadowed with a class attribute.
        super().__init__(name="netwatch-poller", daemon=True)
        self.targets = targets
        self.store = store
        self.interval = interval
        self.timeout = timeout
        self.keep_days = keep_days
        # Named _stop_event, not _stop: threading.Thread has a private
        # _stop() method in CPython <= 3.12, and assigning an Event over it
        # makes the thread raise "'Event' object is not callable" when it
        # exits. Caught by the CI matrix on 3.10-3.12.
        self._stop_event = threading.Event()
        self._last_prune = time.time()

    def run(self) -> None:
        while not self._stop_event.is_set():
            for target in self.targets:
                up, latency, error = probe(
                    target["host"], target["port"], self.timeout)
                self.store.record(target["name"], target["host"],
                                  target["port"], up, latency, error)
            # Housekeeping once an hour so the database does not grow forever.
            if time.time() - self._last_prune > 3600:
                self.store.prune(self.keep_days)
                self._last_prune = time.time()
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
def make_handler(store: Store, targets: list[dict], interval: float):

    class Handler(BaseHTTPRequestHandler):
        server_version = "netwatch/1.0"

        def log_message(self, fmt, *args):  # quieter console
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The dashboard renders server-controlled data only; lock it down.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self'; script-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload, indent=2).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            route = urlparse(self.path)
            path = route.path

            if path == "/api/status":
                self._json({
                    "targets": store.latest(),
                    "configured": len(targets),
                    "interval_seconds": interval,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                })
                return

            if path == "/api/history":
                params = parse_qs(route.query)
                name = (params.get("target") or [""])[0]
                if not name:
                    self._json({"error": "target parameter is required"}, 400)
                    return
                self._json({"target": name, "points": store.history(name)})
                return

            if path == "/healthz":
                self._json({"status": "ok"})
                return

            self._serve_static("index.html" if path == "/" else path.lstrip("/"))

        def _serve_static(self, relative: str) -> None:
            # Resolve and confirm the result stays inside STATIC_DIR, so a
            # crafted path cannot escape the static root.
            full = os.path.realpath(os.path.join(STATIC_DIR, relative))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep):
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            if not os.path.isfile(full):
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            extension = os.path.splitext(full)[1]
            with open(full, "rb") as fh:
                self._send(200, fh.read(),
                           CONTENT_TYPES.get(extension, "application/octet-stream"))

    return Handler


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def load_targets(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    targets = []
    for entry in raw:
        if not {"name", "host", "port"} <= entry.keys():
            raise ValueError(f"target missing name/host/port: {entry}")
        targets.append({"name": str(entry["name"]),
                        "host": str(entry["host"]),
                        "port": int(entry["port"])})
    if not targets:
        raise ValueError("no targets configured")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Network service monitoring with a live web dashboard.")
    parser.add_argument("-c", "--config", default="targets.json",
                        help="JSON file listing targets (default targets.json)")
    parser.add_argument("-d", "--database", default=DEFAULT_DB,
                        help=f"SQLite database path (default {DEFAULT_DB})")
    parser.add_argument("-i", "--interval", type=float, default=30.0,
                        help="seconds between polling rounds (default 30)")
    parser.add_argument("-t", "--timeout", type=float, default=5.0,
                        help="per-probe timeout in seconds (default 5)")
    parser.add_argument("--port", type=int, default=8080,
                        help="dashboard port (default 8080)")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="dashboard bind address (default 127.0.0.1)")
    parser.add_argument("--keep-days", type=int, default=7,
                        help="days of history to retain (default 7)")
    args = parser.parse_args(argv)

    try:
        targets = load_targets(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}")
        return 2

    store = Store(args.database)
    poller = Poller(targets, store, args.interval, args.timeout, args.keep_days)
    poller.start()

    handler = make_handler(store, targets, args.interval)
    server = ThreadingHTTPServer((args.bind, args.port), handler)

    print(f"\n  netwatch monitoring {len(targets)} target(s) every "
          f"{args.interval:g}s")
    print(f"  Dashboard: http://{args.bind}:{args.port}")
    print(f"  Database : {args.database}")
    print("  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
    finally:
        poller.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
