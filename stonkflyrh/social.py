"""The fly's voice on X.

When a trade fills, or the fly leaves a rug or a dead pool, it says so: what,
how much, the market cap, the transaction. Nothing else is posted, nothing
is posted twice, and a failure to post never touches a trade: the post is
attempted after the order is settled and any error is recorded and dropped.

Posting uses the X API v2 `POST /2/tweets` with OAuth 1.0a user context,
signed here with the standard library so the run gains no dependency. Keys
live in .env and are never logged; a run without them writes each would-be
post to the ledger as a dry run, so the wording can be seen before going
live.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse

from .config import D

TWEETS_URL = "https://api.twitter.com/2/tweets"
MIN_SECONDS_BETWEEN_POSTS = 60
MAX_LENGTH = 280

ENV = {
    "api_key": "STONKFLYRH_X_API_KEY",
    "api_secret": "STONKFLYRH_X_API_SECRET",
    "access_token": "STONKFLYRH_X_ACCESS_TOKEN",
    "access_secret": "STONKFLYRH_X_ACCESS_SECRET",
}


def _pct(value):
    return urllib.parse.quote(str(value), safe="~")


def oauth1_header(method, url, creds, params=None, nonce=None, timestamp=None):
    """The Authorization header for one request, per RFC 5849 with HMAC-SHA1.
    `params` are query/form parameters that take part in the signature; a
    JSON body does not."""
    oauth = {
        "oauth_consumer_key": creds["api_key"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(timestamp if timestamp is not None else time.time())),
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    everything = {**(params or {}), **oauth}
    pairs = sorted((_pct(k), _pct(v)) for k, v in everything.items())
    normalised = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join([method.upper(), _pct(url), _pct(normalised)])
    key = f"{_pct(creds['api_secret'])}&{_pct(creds['access_secret'])}".encode()
    signature = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def credentials_from_env():
    creds = {k: os.environ.get(v, "").strip() for k, v in ENV.items()}
    return creds if all(creds.values()) else None


def compact_usd(value):
    n = float(value)
    if n >= 1e9:
        return f"${n / 1e9:.2f}B"
    if n >= 1e6:
        return f"${n / 1e6:.2f}M"
    if n >= 1e3:
        return f"${n / 1e3:.1f}K"
    return f"${n:,.0f}"


class XPoster:
    """Composes and sends the fly's posts. `send` is injectable for tests."""

    def __init__(self, ledger, explorer=None, handle="StonkFlyRH", creds=None, send=None, enabled=True):
        self.l = ledger
        self.explorer = (explorer or "").rstrip("/")
        self.handle = handle
        self.creds = creds
        self.send = send or self._send_http
        self.enabled = enabled
        self.last_post = 0.0

    @property
    def live(self):
        return self.enabled and self.creds is not None

    # -- wording ----------------------------------------------------------------

    def trade_text(self, row, market_cap=None, realised=None):
        ex = row.get("execution") or {}
        side = (row.get("neural") or {}).get("side")
        product = row["product"]
        q = row.get("quote") or {}
        qd = int(q.get("quote_decimals", 6))
        value = D(ex["quote_wei"]) / D(10**qd) if ex.get("quote_wei") else D(str(row.get("notional_usd") or 0))
        forced = ex.get("forced") or (row.get("neural") or {}).get("forced")
        if side == "BUY":
            head = f"🪰 bought ${product} for ${value:.2f}"
        else:
            head = f"🪰 sold ${product} for ${value:.2f}"
            if realised is not None:
                r = D(str(realised))
                head += f" · {'+' if r >= 0 else '-'}${abs(r):.2f} realised"
            if forced:
                head += " · leaving a dead pool" if "dead pool" in str(forced) else f" · {forced}"
        parts = [head]
        if market_cap:
            parts.append(f"MC {compact_usd(market_cap)}")
        parts.append("the fly's connectome decided; the rug screen agreed" if side == "BUY" else "stonkflyrh.com")
        text = " · ".join(parts)
        if self.explorer and ex.get("tx_hash"):
            text += f"\n{self.explorer}/tx/{ex['tx_hash']}"
        return text[:MAX_LENGTH]

    def rug_text(self, record):
        product = record["product"]
        why = record.get("reason", "")
        return f"🪰 ${product} rugged: {why}. Blocklisted; the screen tightens. Losses teach the fly.\nstonkflyrh.com"[:MAX_LENGTH]

    # -- sending ------------------------------------------------------------------

    def post(self, text, kind, ref, now=None):
        """Send once per (kind, ref); record the outcome. Never raises."""
        now = time.time() if now is None else now
        if not self.enabled:
            return None
        key = f"{kind}:{ref}"
        posted = dict(self.l.get("x_posted") or {})
        if key in posted:
            return None
        if now - self.last_post < MIN_SECONDS_BETWEEN_POSTS:
            # Under the floor between posts: skip rather than queue; the site is the record.
            return None
        record = {"at": now, "kind": kind, "ref": ref, "text": text, "live": self.live}
        if not self.live:
            record["status"] = "dry_run"
        else:
            try:
                tweet_id = self.send(text)
                record["status"] = "posted"
                record["tweet_id"] = tweet_id
                record["url"] = f"https://x.com/{self.handle}/status/{tweet_id}" if tweet_id else None
            except Exception as e:
                record["status"] = "failed"
                record["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        self.last_post = now
        posted[key] = {"at": now, "status": record["status"], "tweet_id": record.get("tweet_id")}
        self.l.put("x_posted", posted)
        self.l.record_event("x_posts", record)
        return record

    def _send_http(self, text):
        import urllib.error
        import urllib.request

        header = oauth1_header("POST", TWEETS_URL, self.creds)
        req = urllib.request.Request(
            TWEETS_URL,
            data=json.dumps({"text": text}).encode(),
            headers={"Authorization": header, "Content-Type": "application/json", "User-Agent": "stonkflyrh"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            # Never echo the request; the response body is X's own words.
            raise RuntimeError(f"X answered {e.code}: {e.read().decode(errors='replace')[:200]}") from None
        return (body.get("data") or {}).get("id")
