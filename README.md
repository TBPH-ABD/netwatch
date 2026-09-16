# netwatch

Network service monitoring with a live web dashboard. Polls a set of TCP
services on an interval, records latency and availability to SQLite, and serves
a dashboard plus a JSON API.

**No web framework, no database server, no build step, no dependencies** — the
whole stack is the Python standard library and three static files.

## Features

- **Background poller** measuring TCP connect latency for every target
- **SQLite history** with automatic pruning, so the database never grows forever
- **24-hour uptime and average latency** computed per target
- **Live dashboard** — auto-refreshing tiles, click a target for its latency history
- **JSON API** (`/api/status`, `/api/history`, `/healthz`) for other tooling
- **Responsive and theme-aware** — works on a phone, follows light/dark mode

## Requirements

Python 3.10 or newer. No packages to install.

## Quick start

```bash
# Uses the bundled targets.json
python3 netwatch.py

# Custom config, faster polling, reachable on the LAN
python3 netwatch.py -c production.json -i 15 --bind 0.0.0.0 --port 9000
```

Then open <http://127.0.0.1:8080>.

## Configuration

`targets.json` is a list of services to watch:

```json
[
  { "name": "api-gateway", "host": "10.0.1.20", "port": 443 },
  { "name": "postgres",    "host": "10.0.1.31", "port": 5432 },
  { "name": "redis",       "host": "10.0.1.32", "port": 6379 }
]
```

### Options

| Flag | Description | Default |
| --- | --- | --- |
| `-c`, `--config` | JSON file listing targets | `targets.json` |
| `-d`, `--database` | SQLite database path | `netwatch.db` |
| `-i`, `--interval` | Seconds between polling rounds | `30` |
| `-t`, `--timeout` | Per-probe timeout in seconds | `5` |
| `--port` | Dashboard port | `8080` |
| `--bind` | Dashboard bind address | `127.0.0.1` |
| `--keep-days` | Days of history to retain | `7` |

## API

| Endpoint | Returns |
| --- | --- |
| `GET /api/status` | Latest check for every target, with 24h uptime and average latency |
| `GET /api/history?target=NAME` | The last 60 checks for one target |
| `GET /healthz` | `{"status": "ok"}` — for your own uptime monitoring |

```bash
curl -s localhost:8080/api/status | python3 -m json.tool
```

## Security notes

This is a monitoring tool, so it was built the way a monitoring tool should be:

- **Binds to localhost by default.** Exposing it requires an explicit `--bind`.
- **Path traversal is blocked.** Static paths are resolved with `realpath` and
  rejected unless they land inside the static root.
- **Security headers on every response** — `Content-Security-Policy`,
  `X-Content-Type-Options`, `X-Frame-Options`.
- **No HTML injection.** The dashboard inserts every value as a text node, so
  nothing a monitored host reports back can become markup in the page.
- **Parameterised SQL** everywhere.

There is no authentication layer. Put it behind a reverse proxy or a VPN if you
expose it beyond localhost.

## Design notes

The poller runs in a daemon thread and writes through a lock-guarded SQLite
connection; the HTTP server is a `ThreadingHTTPServer` reading from its own
connections. Uptime and average latency are computed in SQL with correlated
subqueries rather than in Python, so the API stays fast as history grows.

## Tests

40 tests, 81% line coverage. No dependencies, and **no test contacts a
real external service** — network-facing code is exercised against local fake
servers bound to an ephemeral port.

```bash
# Run the suite
python3 -m unittest discover -s tests -v

# Fail on any leaked socket, file, or database connection
python3 -W error::ResourceWarning -m unittest discover -s tests
```

CI runs the suite on Python 3.10–3.13 on every push, plus a coverage gate and a
3.10 syntax check. See [.github/workflows/tests.yml](.github/workflows/tests.yml).

## License

MIT — see [LICENSE](LICENSE).
