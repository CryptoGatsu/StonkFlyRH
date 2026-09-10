"""Transient errors back off; judgement calls halt; the operator's coin is priority."""

import time

import pytest
import requests

from stonkflyrh.cli import TRANSIENT_HALTS, is_transient
from stonkflyrh.config import D, Settings


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.HTTPError("429 Client Error: Too Many Requests"),
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.ReadTimeout("slow"),
        TimeoutError(),
        ConnectionResetError(),
        RuntimeError("upstream returned 503"),
    ],
)
def test_network_failures_are_transient(exc):
    assert is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("USDG balance does not match the ledger"),
        ValueError("bad plan"),
        FileNotFoundError("keystore"),
        PermissionError("keystore"),
    ],
)
def test_judgement_calls_are_not_transient(exc):
    assert not is_transient(exc)


def test_transient_halt_names_are_the_network_ones():
    assert "HTTPError" in TRANSIENT_HALTS and "UnresolvedOrder" not in TRANSIENT_HALTS


@pytest.mark.parametrize("changes", [dict(rpc_error_tolerance=0), dict(rpc_error_backoff_seconds=0),
                                     dict(coin_address="0x123")])
def test_resilience_settings_bounds(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


def test_the_operators_coin_is_admitted_past_the_cap(tmp_path):
    from stonkflyrh.chain import checksum
    from tests.test_discovery import Chain, addr, build, log_for

    coin = checksum(addr(99))
    logs = [log_for(addr(70 + i), 1000 + i) for i in range(3)] + [log_for(coin, 900)]
    ids = {addr(70 + i): (f"C{i}", 18) for i in range(3)}
    ids[coin] = ("FLYCOIN", 18)
    chain = Chain(logs, 1200, ids)
    _, ledger, _, _, disc = build(tmp_path, chain, max_products=2, coin_address=coin)
    try:
        report = disc.scan(time.time(), D("2500"))
        added = [a["symbol"] for a in report["added"]]
        # PONS is the seed; only one slot remains — and the coin takes none.
        assert "FLYCOIN" in added and len(added) == 2
    finally:
        ledger.close()
