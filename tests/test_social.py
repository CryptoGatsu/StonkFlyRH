"""The fly's posts on X: the OAuth signature against X's own documented vector,
the wording, once-per-event posting, dry runs, and failures that never raise."""

from stonkflyrh.config import D, Settings
from stonkflyrh.ledger import Ledger
from stonkflyrh.social import ENV, XPoster, compact_usd, oauth1_header


def test_oauth1_signature_matches_the_documented_example():
    """X's developer docs sign this exact request; the header must agree."""
    creds = {
        "api_key": "xvz1evFS4wEEPTGEFPHBog",
        "api_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "access_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "access_secret": "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    }
    header = oauth1_header(
        "POST", "https://api.twitter.com/1.1/statuses/update.json", creds,
        params={"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp=1318622958,
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
    assert header.startswith("OAuth ") and 'oauth_signature_method="HMAC-SHA1"' in header


def row(side, tx="0x" + "ab" * 32, forced=None):
    return {
        "product": "WOOF", "tick": 7, "wall_time": 1.0,
        "neural": {"side": side, **({"forced": forced} if forced else {})},
        "quote": {"quote_decimals": 6},
        "execution": {"status": "SETTLED", "tx_hash": tx, "quote_wei": "9990000", **({"forced": forced} if forced else {})},
        "pnl_delta_usd": "0.42",
    }


def test_wording_for_a_buy_a_sell_and_a_forced_exit(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", D("100"))
    try:
        p = XPoster(ledger, explorer="https://scan.example")
        buy = p.trade_text(row("BUY"), market_cap=84000)
        assert buy.startswith("🪰 bought $WOOF for $9.99") and "MC $84.0K" in buy
        assert buy.endswith("https://scan.example/tx/0x" + "ab" * 32)
        sell = p.trade_text(row("SELL"), realised="0.42")
        assert "sold $WOOF for $9.99 · +$0.42 realised" in sell and "stonkflyrh.com" in sell
        exit_ = p.trade_text(row("SELL", forced="no swaps in the last 4.0h; leaving a dead pool"), realised="-0.70")
        assert "-$0.70 realised · leaving a dead pool" in exit_
        assert len(exit_) <= 280
        assert compact_usd(2_300_000) == "$2.30M"
    finally:
        ledger.close()


def test_posts_once_per_event_and_records_dry_runs_and_failures(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", D("100"))
    try:
        sent = []
        dry = XPoster(ledger, creds=None, send=lambda text: sent.append(text) or "1")
        record = dry.post("hello", "trade", "0xaaa", now=1000.0)
        assert record["status"] == "dry_run" and sent == []
        assert ledger.events("x_posts")[0]["text"] == "hello"
        assert dry.post("hello again", "trade", "0xaaa", now=2000.0) is None     # same event: once

        live = XPoster(ledger, creds={"api_key": "k", "api_secret": "s", "access_token": "t", "access_secret": "x"},
                       send=lambda text: sent.append(text) or "17")
        record = live.post("world", "trade", "0xbbb", now=3000.0)
        assert record["status"] == "posted" and record["url"].endswith("/status/17") and sent == ["world"]
        assert live.post("too soon", "trade", "0xccc", now=3010.0) is None          # under the floor

        def boom(text):
            raise RuntimeError("X answered 403: forbidden")

        failing = XPoster(ledger, creds=live.creds, send=boom)
        record = failing.post("nope", "trade", "0xddd", now=5000.0)
        assert record["status"] == "failed" and "403" in record["error"]         # recorded, not raised
    finally:
        ledger.close()


def test_tick_hook_posts_filled_trade_and_rug_once(tmp_path):
    from decimal import Decimal
    from stonkflyrh.cli import _market_cap, _say_on_x

    class Ledger:
        def __init__(self):
            self.meta, self.events = {}, []

        def get(self, k):
            return self.meta.get(k)

        def put(self, k, v):
            self.meta[k] = v

        def record_event(self, kind, payload):
            self.events.append((kind, payload))

        def universe(self):
            return {"PEPE": {"total_supply": str(10**9 * 10**18), "decimals": 18}}

        def dropped(self):
            return {}

    class Q:
        bid, ask = Decimal("0.001"), Decimal("0.0011")

    sent = []
    ledger = Ledger()
    poster = XPoster(ledger, explorer="https://ex", creds={k: "x" for k in ENV}, send=lambda t: sent.append(t) or "7")
    row = {"tick": 3, "product": "PEPE", "neural": {"side": "BUY"},
           "execution": {"status": "SETTLED", "tx_hash": "0xabc", "quote_wei": "10000000"},
           "quote": {"quote_decimals": 6}, "notional_usd": "10"}
    assert _market_cap(ledger.universe()["PEPE"], Decimal("0.00105")) == Decimal("1050000")
    _say_on_x(poster, row, None, ledger, Q)
    _say_on_x(poster, row, None, ledger, Q)  # the same fill again: no second post
    assert len(sent) == 1 and "bought $PEPE for $10.00" in sent[0] and "MC $1.05M" in sent[0]
    assert "https://ex/tx/0xabc" in sent[0]
    poster.last_post = 0
    rug = {"product": "PEPE", "at": 1.0, "reason": "bid fell 55% below entry"}
    _say_on_x(poster, {**row, "execution": {"status": "HOLD"}}, rug, ledger, Q)
    assert len(sent) == 2 and "$PEPE rugged" in sent[1]
    assert [k for k, _ in ledger.events] == ["x_posts", "x_posts"]
