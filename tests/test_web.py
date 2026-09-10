"""The live site is read-only. These tests assert it stays that way."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from stonkflyrh.broker import PaperBroker
from stonkflyrh.config import Settings
from stonkflyrh.ledger import Ledger
from stonkflyrh.risk import Guard
from stonkflyrh.web import server as web
from tests.test_execution import CAPITAL, ETH_USD, quote


def write_run(tmp_path, ticks=3):
    settings = Settings(protocol_fee_bps=100)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper", CAPITAL)
    guard = Guard(settings, ledger, tmp_path / "STOP")
    broker = PaperBroker(settings, ledger, {"trading": "0xaaa"})
    rows = []
    for tick in range(1, ticks + 1):
        ledger.put("last_attempt", 0)
        plan = ledger.reserve(
            guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time()
        )
        execution = broker.execute(plan, guard.before_submit)
        rows.append(
            {
                "tick": tick,
                "wall_time": time.time(),
                "product": "PONS",
                "mode": "paper",
                "quote": quote().json(),
                "equity_weth": str(ledger.cash),
                "equity_usd": "100.00",
                "notional_usd": "10.00",
                "screen": {"approved": True, "checks": [
                    {"name": "sellable", "passed": True, "detail": "ok"},
                    {"name": "liquidity", "passed": True, "detail": "ok"},
                ]},
                "neural": {"side": "BUY", "difference_hz": 1.5, "gate_spikes": 4,
                           "stimulus": "reward", "memory": {"changed_edges": 12}},
                "execution": execution,
            }
        )
        ledger.commit_tick(ledger.cash, None)
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "mode": "paper",
                "feed": "fixture",
                "network": {"name": "Robinhood Chain", "chain_id": 4663,
                            "explorer": "https://explorer.example"},
                "wallets": {"fly": "0xaaa", "fee": "0xbbb"},
                "screen": {"enabled": True},
                "social": {"configured": False, "opted_in": False, "live": False},
            }
        )
    )
    (tmp_path / "latest-input.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ledger.close()
    return rows


@pytest.fixture
def site(tmp_path):
    write_run(tmp_path)
    httpd = web.serve(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, tmp_path
    httpd.stopping.set()
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_index_is_served(site):
    base, _ = site
    status, kind, body = get(base + "/")
    assert status == 200
    assert kind.startswith("text/html")
    assert b"StonkFlyRH" in body


def test_state_reports_mode_fees_and_the_dev_share(site):
    base, _ = site
    _, _, body = get(base + "/api/state")
    state = json.loads(body)
    fees = state["meta"]["fees"]
    assert state["meta"]["mode"] == "paper"
    assert fees["fills_charged"] == 3
    assert int(fees["gross_wei"]) > 0
    assert state["provenance"]["wallets"]["fee"] == "0xbbb"
    assert state["meta"]["blocklist"] == []
    assert state["meta"]["rugs"] == []


def test_state_omits_the_bulky_observation_blob(site):
    base, _ = site
    _, _, body = get(base + "/api/state")
    assert "observation" not in json.loads(body)["meta"]


def test_trades_returns_rows_and_supports_since(site):
    base, _ = site
    _, _, body = get(base + "/api/trades")
    trades = json.loads(body)["trades"]
    assert [t["tick"] for t in trades] == [1, 2, 3]
    _, _, body = get(base + "/api/trades?since=2")
    assert [t["tick"] for t in json.loads(body)["trades"]] == [3]


def test_latest_frame_is_served(site):
    base, _ = site
    status, kind, body = get(base + "/latest-input.png")
    assert status == 200 and kind == "image/png"
    assert body.startswith(b"\x89PNG")


def test_stream_pushes_a_new_trade(site):
    base, out = site
    # The client resumes from the last tick it rendered, so a row written the
    # instant the stream opens is still delivered.
    request = urllib.request.Request(base + "/api/stream?since=3")
    with urllib.request.urlopen(request, timeout=10) as stream:
        with (out / "events.jsonl").open("a") as f:
            f.write(json.dumps({"tick": 99, "wall_time": time.time(),
                                "product": "PONS", "neural": {"side": "SELL"},
                                "execution": {"status": "FILLED"}}) + "\n")
        deadline = time.time() + 8
        payload = b""
        while time.time() < deadline and b'"tick": 99' not in payload:
            payload += stream.readline()
    assert b"event: trade" in payload
    assert b'"tick": 99' in payload


@pytest.mark.parametrize(
    "path",
    [
        "/ledger.sqlite",
        "/keystore/trading.json",
        "/static/../../ledger.sqlite",
        "/api/nope",
        "/provenance.json",
    ],
)
def test_no_route_reaches_run_state_or_keys(site, path):
    base, _ = site
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + path)
    assert e.value.code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_the_site_accepts_no_writes(site, method):
    base, _ = site
    request = urllib.request.Request(base + "/api/state", data=b"{}", method=method)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(request, timeout=5)
    assert e.value.code in (404, 501)


def test_missing_run_directory_still_answers(tmp_path):
    httpd = web.serve(tmp_path / "absent", "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        _, _, body = get(base + "/api/state")
        assert json.loads(body)["meta"] == {}
        _, _, body = get(base + "/api/trades")
        assert json.loads(body)["trades"] == []
    finally:
        httpd.stopping.set()
        httpd.shutdown()
        httpd.server_close()
