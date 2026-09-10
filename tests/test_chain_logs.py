"""ChainClient.logs against a fake node that rate-limits and caps ranges."""

import pytest

from stonkflyrh.chain import ChainClient, network


class Eth:
    chain_id = 4663

    def __init__(self, fail_first=0, max_range=None):
        self.calls = []
        self.fail_first = fail_first
        self.max_range = max_range

    def get_logs(self, q):
        self.calls.append((q["fromBlock"], q["toBlock"]))
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("HTTP 429 Too Many Requests")
        if self.max_range and q["toBlock"] - q["fromBlock"] + 1 > self.max_range:
            raise ValueError("query returned more than 10000 results; range too large")
        return [{"blockNumber": q["fromBlock"]}]


def client(eth):
    w3 = type("W3", (), {"eth": eth})()
    return ChainClient(network("robinhood-mainnet"), w3=w3)


def test_a_range_is_walked_in_chunks_and_capped():
    eth = Eth()
    c = client(eth)
    out, hi = c.logs({"fromBlock": 1, "toBlock": 10000}, chunk=2000, pause=0, max_blocks=6000)
    assert hi == 6000
    assert eth.calls == [(1, 2000), (2001, 4000), (4001, 6000)]
    assert len(out) == 3


def test_transient_errors_are_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    eth = Eth(fail_first=2)
    c = client(eth)
    out, _ = c.logs({"fromBlock": 1, "toBlock": 100}, chunk=2000, pause=0.01)
    assert len(out) == 1 and len(eth.calls) == 3


def test_persistent_errors_eventually_raise(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    eth = Eth(fail_first=99)
    c = client(eth)
    with pytest.raises(RuntimeError, match="429"):
        c.logs({"fromBlock": 1, "toBlock": 100}, chunk=2000, pause=0.01, retries=3)


def test_the_chunk_halves_when_the_node_caps_the_range():
    eth = Eth(max_range=500)
    c = client(eth)
    out, hi = c.logs({"fromBlock": 1, "toBlock": 1000}, chunk=2000, pause=0)
    assert hi == 1000
    assert all(b - a + 1 <= 500 for a, b in eth.calls if (a, b) not in [(1, 2000), (1, 1000)])
    assert len(out) == 2
