"""Posting the fly's trades to X.

Every post is written to `posts.jsonl` in the run directory first and sent
second, so the record of what the fly said is local and survives a failed send.
Posting is off unless credentials are present *and* the run opts in, and a send
failure is never allowed to interrupt trading: the worker's job is the ledger,
not the timeline.

Credentials are read from the environment, used to sign, and never logged,
never written to the run directory, and never served by the website.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

ENDPOINT = "https://api.x.com/2/tweets"
LIMIT = 280
OPT_IN = "STONKFLYRH_POST_TO_X"
CREDENTIALS = (
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_SECRET",
)


def credentials():
    values = {name: os.environ.get(name) for name in CREDENTIALS}
    return values if all(values.values()) else None


def quote(value):
    return urllib.parse.quote(str(value), safe="~")


def oauth1_header(method, url, creds, nonce=None, timestamp=None):
    """OAuth 1.0a HMAC-SHA1, which is what X still wants for automation.

    The JSON body is not part of the signature base for a POST with a JSON
    content type, so only the OAuth parameters are signed.
    """
    params = {
        "oauth_consumer_key": creds["X_API_KEY"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(timestamp or time.time())),
        "oauth_token": creds["X_ACCESS_TOKEN"],
        "oauth_version": "1.0",
    }
    encoded = "&".join(
        f"{quote(k)}={quote(params[k])}" for k in sorted(params)
    )
    base = "&".join([method.upper(), quote(url), quote(encoded)])
    key = f"{quote(creds['X_API_SECRET'])}&{quote(creds['X_ACCESS_SECRET'])}".encode()
    signature = base64.b64encode(
        hmac.new(key, base.encode(), hashlib.sha1).digest()
    ).decode()
    params["oauth_signature"] = signature
    return "OAuth " + ", ".join(
        f'{quote(k)}="{quote(params[k])}"' for k in sorted(params)
    )


def money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$?"


def price(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{v:.4e}" if v and v < 1e-4 else f"{v:.6g}"


def fit(text, url=None):
    """Trim the body so the whole post, link included, fits X's limit."""
    # X counts every link as a fixed 23 characters regardless of its length.
    room = LIMIT - (24 if url else 0)
    body = text if len(text) <= room else text[: max(0, room - 1)].rstrip() + "…"
    return f"{body}\n{url}" if url else body


@dataclass
class Post:
    kind: str
    text: str
    at: float
    status: str = "DRAFT"
    url: str = None
    error: str = None

    def json(self):
        return {
            "kind": self.kind,
            "text": self.text,
            "at": self.at,
            "status": self.status,
            "url": self.url,
            "error": self.error,
        }


def compose_trade(row, explorer=None):
    """One executed trade as a post. Returns None for nothing worth saying."""
    execution = row.get("execution") or {}
    status = execution.get("status")
    if status not in ("FILLED", "SETTLED"):
        return None
    plan = row.get("plan") or {}
    side = (row.get("neural") or {}).get("side", "?")
    product = row.get("product", "?")
    verb = "bought" if side == "BUY" else "sold"
    size = plan.get("notional_usd") or row.get("notional_usd")
    quote_row = row.get("quote") or {}
    mark = quote_row.get("ask") if side == "BUY" else quote_row.get("bid")
    lines = [
        f"🪰 {verb} {money(size)} of ${product} at {price(mark)} WETH",
        f"equity {money(row.get('equity_usd'))} · tick {row.get('tick')}",
    ]
    screen = row.get("screen") or {}
    if screen.get("checks"):
        passed = sum(1 for c in screen["checks"] if c.get("passed"))
        lines.append(f"rug screen {passed}/{len(screen['checks'])} clear")
    tx = execution.get("tx_hash")
    url = f"{explorer.rstrip('/')}/tx/{tx}" if tx and explorer else None
    return Post("trade", fit("\n".join(lines), url), row.get("wall_time") or time.time())


def compose_rug(record, explorer=None):
    product = record.get("product", "?")
    drawdown = record.get("drawdown")
    try:
        percent = f"{float(drawdown) * 100:.0f}%"
    except (TypeError, ValueError):
        percent = "?"
    lines = [
        f"🚨 rugged on ${product} — bid fell {percent} below entry.",
        "blocklisted for the rest of the run, and the aversive pulse into the "
        "fly's PPL101 cells runs long for this one.",
        "screening got stricter for whatever comes next.",
    ]
    return Post("rug", fit("\n".join(lines)), record.get("at") or time.time())


def compose_screen_block(product, reason):
    return Post(
        "screen",
        fit(f"🛑 skipped ${product} — {reason}"),
        time.time(),
    )


class XPoster:
    """Writes every post locally, and sends it when the run has opted in."""

    def __init__(self, out, settings=None, min_interval=90, opener=None):
        from pathlib import Path

        self.path = Path(out) / "posts.jsonl"
        self.creds = credentials()
        self.opted_in = os.environ.get(OPT_IN) == "1"
        self.min_interval = min_interval
        self.opener = opener or urllib.request.urlopen
        self.last_sent = 0.0
        self.seen = set()

    @property
    def live(self):
        return bool(self.creds) and self.opted_in

    def status(self):
        return {
            "configured": bool(self.creds),
            "opted_in": self.opted_in,
            "live": self.live,
            "min_interval_seconds": self.min_interval,
        }

    def publish(self, post, now=None):
        """Record, then try to send. A send failure is recorded, not raised."""
        if post is None:
            return None
        now = time.time() if now is None else now
        digest = hashlib.sha256(post.text.encode()).hexdigest()
        if digest in self.seen:
            return None
        self.seen.add(digest)
        if not self.live:
            post.status = "DRAFT"
        elif now - self.last_sent < self.min_interval:
            post.status = "THROTTLED"
        else:
            try:
                post.url = self._send(post.text)
                post.status = "SENT"
                self.last_sent = now
            except Exception as e:
                # Never surface the exception text: it can echo a signed header.
                post.status = "FAILED"
                post.error = type(e).__name__
        self._append(post)
        return post

    def _append(self, post):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(post.json(), allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _send(self, text):
        body = json.dumps({"text": text}).encode()
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": oauth1_header("POST", ENDPOINT, self.creds),
                "Content-Type": "application/json",
            },
        )
        with self.opener(request, timeout=10) as response:
            payload = json.loads(response.read().decode())
        posted = (payload.get("data") or {}).get("id")
        return f"https://x.com/i/web/status/{posted}" if posted else None


def read_posts(out, limit=50):
    from pathlib import Path

    path = Path(out) / "posts.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
