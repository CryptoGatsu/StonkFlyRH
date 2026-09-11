"""The operator's `sell`: a request file the worker acts on, whole, at its next tick."""

import json
from argparse import Namespace

import pytest

from stonkflyrh.cli import _sell_requests, cmd_sell
from stonkflyrh.config import D, Settings
from stonkflyrh.ledger import Ledger
from stonkflyrh.market import Quote
from stonkflyrh.risk import Guard


def test_sell_leaves_a_request_only_for_a_held_coin(tmp_path, capsys):
    settings = Settings(products=("RETAIL", "KEKIUS"))
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live", D("100"))
    try:
        ledger.put("positions", {"RETAIL": "1234.5"})
    finally:
        ledger.close()
    with pytest.raises(RuntimeError, match="holds no KEKIUS. Held: RETAIL"):
        cmd_sell(Namespace(product="kekius", out=tmp_path, block=False, cancel=False))
    cmd_sell(Namespace(product="retail", out=tmp_path, block=True, cancel=False))
    said = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert said["sell"] == "RETAIL" and said["held"] == "1234.5" and said["block"] is True
    assert _sell_requests(tmp_path) == {"RETAIL": {"product": "RETAIL", "block": True, "at": pytest.approx(said and json.loads((tmp_path / "SELL-RETAIL").read_text())["at"])}}
    cmd_sell(Namespace(product="RETAIL", out=tmp_path, block=False, cancel=True))
    assert _sell_requests(tmp_path) == {} and not (tmp_path / "SELL-RETAIL").exists()


def test_a_requested_exit_sells_the_whole_position_through_the_exit_spread(tmp_path):
    settings = Settings(products=("RETAIL",))
    ledger = Ledger(tmp_path / "l.sqlite", settings, "paper", D("100"))
    try:
        ledger.commit_tick(ledger.cash, None)
        ledger.put("positions", {"RETAIL": "50000"})
        guard = Guard(settings, ledger, tmp_path / "STOP")
        q = Quote("RETAIL", D("0.001"), D("0.00101"), 1000.0, 18, 6, 30000, D("0.001005"), D("1"))
        # Without a request a sell is a slice of the position, one order's worth.
        slice_ = guard.plan("RETAIL", "SELL", {"RETAIL": q}, D("3000"), now=1000.0, gas_price_wei=None, history=[0.001] * 5)
        assert D(slice_["amount_in_wei"]) < D(50000) * 10**18
        guard.exit_requests = {"RETAIL"}
        assert guard.spread_limit("RETAIL", "SELL") == D(settings.rug_exit_spread)
        whole = guard.plan("RETAIL", "SELL", {"RETAIL": q}, D("3000"), now=1000.0, gas_price_wei=None, history=[0.001] * 5)
        assert D(whole["amount_in_wei"]) == D(50000) * 10**18
        assert not ledger.is_blocked("RETAIL")
    finally:
        ledger.close()
