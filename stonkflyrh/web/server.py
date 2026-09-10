"""Read-only live trade site for a running StonkFlyRH worker.

The worker already writes an append-only `events.jsonl` and a SQLite ledger.
This server tails those and nothing else: it never imports the broker, never
holds a key, never opens the keystore directory, and exposes no route that can
place, cancel or alter a trade. The worker is unaffected if the site is not
running, and the site is safe to leave running when the worker is stopped.

Served on 127.0.0.1 by default. `--host 0.0.0.0` is available for putting it
behind a reverse proxy you control; it publishes wallet addresses, balances and
trade history, so treat that as publishing.
"""

import json
import mimetypes
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).with_name("static")
MAX_TRADES = 500
# Only these names are ever read out of the run directory. The keystore and the
# ledger's raw files are not reachable through any route.
ASSETS = {"/latest-input.png": "latest-input.png"}


def read_meta(out):
    path = out / "ledger.sqlite"
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
        fees = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(CAST(gross_wei AS INTEGER)),0) FROM fees"
        ).fetchone()
        blocklist = [
            {"product": r[0], "at": r[1], "reason": r[2]}
            for r in db.execute("SELECT product,at,reason FROM blocklist ORDER BY at DESC")
        ]
        rugs = [
            json.loads(r[0])
            for r in db.execute("SELECT record FROM rugs ORDER BY id DESC LIMIT 20")
        ]
        screens = {}
        for product, verdict in db.execute("SELECT product,verdict FROM screens"):
            try:
                screens[product] = json.loads(verdict)
            except json.JSONDecodeError:
                continue
        payouts = [
            {
                "created": r[0],
                "beneficiary": r[1],
                "destination": r[2],
                "amount_wei": r[3],
                "status": r[4],
                "tx_hash": r[5],
            }
            for r in db.execute(
                "SELECT created,beneficiary,destination,amount_wei,status,tx_hash "
                "FROM fee_payouts ORDER BY id DESC LIMIT 20"
            )
        ]
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    # The observation blob carries the full price history and is large; the
    # dashboard reads history from the trade rows instead.
    meta.pop("observation", None)
    meta["fees"] = {"fills_charged": int(fees[0]), "gross_wei": str(int(fees[1]))}
    meta["payouts"] = payouts
    meta["blocklist"] = blocklist
    meta["rugs"] = rugs
    meta["screens"] = screens
    return meta


def read_trades(out, limit=MAX_TRADES):
    path = out / "events.jsonl"
    if not path.exists():
        return []
    with path.open("r") as f:
        lines = f.readlines()[-limit:]
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def snapshot(out):
    meta = read_meta(out)
    provenance = {}
    p = out / "provenance.json"
    if p.exists():
        try:
            full = json.loads(p.read_text())
            provenance = {
                k: full.get(k)
                for k in [
                    "mode",
                    "feed",
                    "network",
                    "wallets",
                    "usd_reference",
                    "screen",
                    "social",
                    "settings",
                ]
            }
        except json.JSONDecodeError:
            pass
    return {
        "server_time": time.time(),
        "run": str(out),
        "meta": meta,
        "provenance": provenance,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "stonkflyrh"
    protocol_version = "HTTP/1.1"
    out = Path("runs/paper")

    def log_message(self, *_):
        pass

    def _send(self, code, body, content_type="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value, code=200):
        self._send(code, json.dumps(value, allow_nan=False), "application/json")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        route = url.path
        query = parse_qs(url.query)
        if route in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if route == "/api/state":
            return self._json(snapshot(self.out))
        if route == "/api/trades":
            since = int((query.get("since") or ["0"])[0])
            rows = [r for r in read_trades(self.out) if int(r.get("tick", 0)) > since]
            return self._json({"trades": rows})
        if route == "/api/posts":
            from ..social import read_posts

            return self._json({"posts": read_posts(self.out)})
        if route == "/api/stream":
            since = query.get("since")
            return self._stream(int(since[0]) if since else None)
        if route in ASSETS:
            return self._file(self.out / ASSETS[route], missing_ok=True)
        if route.startswith("/static/"):
            name = route[len("/static/") :]
            target = (STATIC / name).resolve()
            if target.is_file() and target.is_relative_to(STATIC.resolve()):
                return self._file(target)
        return self._json({"error": "not found"}, 404)

    def _file(self, path, missing_ok=False):
        path = Path(path)
        if not path.is_file():
            if missing_ok:
                return self._json({"error": "not yet written"}, 404)
            return self._json({"error": "not found"}, 404)
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return self._send(200, path.read_bytes(), kind)

    def _stream(self, since=None):
        """Server-sent events: one message per new trade row.

        The client passes the last tick it already rendered. Without that, a row
        written between the response headers and the first read would never be
        sent, and the page would sit one trade behind until it was reloaded.
        """
        seen = (
            since
            if since is not None
            else max((int(r.get("tick", 0)) for r in read_trades(self.out)), default=0)
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_state = 0.0
        try:
            while not self.server.stopping.is_set():
                for row in read_trades(self.out, limit=50):
                    tick = int(row.get("tick", 0))
                    if tick > seen:
                        seen = tick
                        self._event("trade", row)
                now = time.time()
                if now - last_state > 5:
                    last_state = now
                    self._event("state", snapshot(self.out))
                    from ..social import read_posts

                    self._event("posts", {"posts": read_posts(self.out, limit=20)})
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _event(self, name, payload):
        body = f"event: {name}\ndata: {json.dumps(payload, allow_nan=False)}\n\n"
        self.wfile.write(body.encode())
        self.wfile.flush()


def serve(out, host="127.0.0.1", port=8787):
    handler = type("BoundHandler", (Handler,), {"out": Path(out)})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.stopping = threading.Event()
    return httpd
