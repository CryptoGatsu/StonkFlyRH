"""Posting to X. No test contacts the network; the opener is a double."""

import json
import time

import pytest

from stonkflyrh.social import (
    LIMIT,
    XPoster,
    compose_rug,
    compose_screen_block,
    compose_trade,
    credentials,
    fit,
    oauth1_header,
    read_posts,
)

EXPLORER = "https://explorer.example"
CREDS = {
    "X_API_KEY": "key",
    "X_API_SECRET": "secret",
    "X_ACCESS_TOKEN": "token",
    "X_ACCESS_SECRET": "tokensecret",
}


def trade_row(**changes):
    row = {
        "tick": 12,
        "wall_time": time.time(),
        "product": "PONS",
        "equity_usd": "104.25",
        "notional_usd": "9.87",
        "quote": {"bid": "9.8e-8", "ask": "1.01e-7"},
        "neural": {"side": "BUY"},
        "screen": {"approved": True, "checks": [{"passed": True}, {"passed": True}]},
        "execution": {"status": "FILLED"},
    }
    row.update(changes)
    return row


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    for name in CREDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("STONKFLYRH_POST_TO_X", raising=False)


# -- composing -------------------------------------------------------------


def test_a_filled_buy_reads_as_a_trade_post():
    post = compose_trade(trade_row(), EXPLORER)
    assert post.kind == "trade"
    assert "bought" in post.text
    assert "$9.87" in post.text
    assert "$PONS" in post.text
    assert "2/2 clear" in post.text


def test_a_sell_says_sold():
    post = compose_trade(trade_row(neural={"side": "SELL"}))
    assert "sold" in post.text


def test_a_hold_or_veto_is_not_posted():
    assert compose_trade(trade_row(execution={"status": "HOLD"})) is None
    assert compose_trade(trade_row(execution={"status": "VETO"})) is None


def test_a_live_trade_carries_its_explorer_link():
    row = trade_row(execution={"status": "SETTLED", "tx_hash": "0x" + "ab" * 32})
    post = compose_trade(row, EXPLORER)
    assert post.text.endswith(EXPLORER + "/tx/0x" + "ab" * 32)


def test_a_rug_post_says_what_happened():
    post = compose_rug({"product": "SCAM", "drawdown": "0.72", "at": 0})
    assert post.kind == "rug"
    assert "$SCAM" in post.text
    assert "72%" in post.text
    assert "blocklisted" in post.text


def test_a_screen_block_post_names_the_reason():
    post = compose_screen_block("SCAM", "liquidity: pool holds about $900")
    assert "skipped $SCAM" in post.text
    assert "liquidity" in post.text


def test_every_composed_post_fits_the_limit():
    long_row = trade_row(product="A" * 12, equity_usd="1" * 30)
    for post in [
        compose_trade(long_row, EXPLORER),
        compose_rug({"product": "B" * 12, "drawdown": "0.9", "at": 0}),
        compose_screen_block("C" * 12, "x" * 400),
    ]:
        assert len(post.text) <= LIMIT


def test_fit_reserves_room_for_a_link():
    text = fit("y" * 400, "https://example.com/" + "z" * 200)
    body = text.split("\n")[0]
    assert len(body) <= LIMIT - 24


# -- signing ---------------------------------------------------------------


def test_the_oauth_header_is_deterministic_and_carries_a_signature():
    header = oauth1_header("POST", "https://api.x.com/2/tweets", CREDS, "abc", 1700000000)
    again = oauth1_header("POST", "https://api.x.com/2/tweets", CREDS, "abc", 1700000000)
    assert header == again
    assert header.startswith("OAuth ")
    assert "oauth_signature=" in header
    assert 'oauth_consumer_key="key"' in header


def test_a_different_nonce_changes_the_signature():
    a = oauth1_header("POST", "https://api.x.com/2/tweets", CREDS, "abc", 1700000000)
    b = oauth1_header("POST", "https://api.x.com/2/tweets", CREDS, "def", 1700000000)
    assert a != b


def test_credentials_need_every_field(monkeypatch):
    assert credentials() is None
    for name, value in CREDS.items():
        monkeypatch.setenv(name, value)
    assert credentials() == CREDS
    monkeypatch.delenv("X_ACCESS_SECRET")
    assert credentials() is None


# -- publishing ------------------------------------------------------------


def test_without_credentials_posts_are_drafted_locally(tmp_path):
    poster = XPoster(tmp_path)
    assert not poster.live
    post = poster.publish(compose_trade(trade_row()))
    assert post.status == "DRAFT"
    assert len(read_posts(tmp_path)) == 1


def test_credentials_without_opt_in_do_not_send(tmp_path, monkeypatch):
    for name, value in CREDS.items():
        monkeypatch.setenv(name, value)
    poster = XPoster(tmp_path)
    assert not poster.live
    assert poster.publish(compose_trade(trade_row())).status == "DRAFT"


def opted_in(tmp_path, monkeypatch, opener):
    for name, value in CREDS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("STONKFLYRH_POST_TO_X", "1")
    return XPoster(tmp_path, opener=opener)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_an_opted_in_run_sends_and_records_the_url(tmp_path, monkeypatch):
    sent = []

    def opener(request, timeout=None):
        sent.append(request)
        return Response({"data": {"id": "1899"}})

    poster = opted_in(tmp_path, monkeypatch, opener)
    post = poster.publish(compose_trade(trade_row()))
    assert post.status == "SENT"
    assert post.url == "https://x.com/i/web/status/1899"
    assert sent[0].headers["Authorization"].startswith("OAuth ")
    assert json.loads(sent[0].data)["text"] == post.text


def test_a_send_failure_is_recorded_and_never_raised(tmp_path, monkeypatch):
    def opener(_request, timeout=None):
        raise OSError("api down, token=secret")

    poster = opted_in(tmp_path, monkeypatch, opener)
    post = poster.publish(compose_trade(trade_row()))
    assert post.status == "FAILED"
    # The exception text could echo a signed header, so only its type is kept.
    assert post.error == "OSError"
    assert "secret" not in json.dumps(post.json())


def test_posts_are_throttled(tmp_path, monkeypatch):
    poster = opted_in(tmp_path, monkeypatch, lambda *a, **k: Response({"data": {"id": "1"}}))
    now = time.time()
    first = poster.publish(compose_trade(trade_row(tick=1)), now)
    second = poster.publish(compose_trade(trade_row(tick=2)), now + 5)
    assert first.status == "SENT"
    assert second.status == "THROTTLED"


def test_an_identical_post_is_not_repeated(tmp_path):
    poster = XPoster(tmp_path)
    row = trade_row()
    assert poster.publish(compose_trade(row)) is not None
    assert poster.publish(compose_trade(row)) is None
    assert len(read_posts(tmp_path)) == 1


def test_publishing_nothing_is_a_no_op(tmp_path):
    assert XPoster(tmp_path).publish(None) is None
    assert read_posts(tmp_path) == []


def test_status_reports_why_it_is_not_live(tmp_path):
    status = XPoster(tmp_path).status()
    assert status == {
        "configured": False,
        "opted_in": False,
        "live": False,
        "min_interval_seconds": 90,
    }


def test_read_posts_skips_a_torn_line(tmp_path):
    (tmp_path / "posts.jsonl").write_text('{"text": "ok"}\n{"text": incomplete\n')
    assert [p["text"] for p in read_posts(tmp_path)] == ["ok"]
