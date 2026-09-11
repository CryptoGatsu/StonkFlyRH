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


def test_gas_price_clears_the_current_base_fee_with_headroom():
    """The node refused a transaction priced at its own suggestion because the
    base fee ticked above it a block later."""
    from stonkflyrh.chain import ChainClient

    c = ChainClient.__new__(ChainClient)
    c.w3 = type("W3", (), {})()
    c.w3.eth = type("Eth", (), {
        "gas_price": 127_364_000,
        "get_block": staticmethod(lambda tag: {"baseFeePerGas": 127_948_000}),
    })()
    assert c.gas_price() == int(127_948_000 * 1.25)
    c.w3.eth = type("Eth", (), {
        "gas_price": 200_000_000,
        "get_block": staticmethod(lambda tag: {}),      # a node without base fees
    })()
    assert c.gas_price() == 250_000_000


def test_the_chunk_halves_when_a_hosted_endpoint_refuses_a_large_reply(monkeypatch):
    """QuickNode answers HTTP 413 when one reply would be too big."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    class Capped(Eth):
        def get_logs(self, q):
            self.calls.append((q["fromBlock"], q["toBlock"]))
            if q["toBlock"] - q["fromBlock"] + 1 > 500:
                raise RuntimeError("413 Client Error: Request Entity Too Large for url: https://x")
            return [{"blockNumber": q["fromBlock"]}]

    eth = Capped()
    c = client(eth)
    out, hi = c.logs({"fromBlock": 1, "toBlock": 1000}, chunk=2000, pause=0)
    assert hi == 1000 and len(out) == 2
    assert eth.calls[-1][1] - eth.calls[-1][0] + 1 <= 500


def test_get_logs_moves_to_the_fallback_when_the_primary_refuses_it(monkeypatch):
    """QuickNode answered 413 to every eth_getLogs on this chain; the public node
    serves the method, so logs go there while everything else stays put."""
    from stonkflyrh.chain import ChainClient

    class Refusing(Eth):
        def get_logs(self, q):
            self.calls.append((q["fromBlock"], q["toBlock"]))
            raise RuntimeError("413 Client Error: Request Entity Too Large for url: https://primary")

    primary, public = Refusing(), Eth()
    c = client(primary)
    monkeypatch.setenv("STONKFLYRH_RPC_URL", "https://primary.example")
    monkeypatch.setattr(ChainClient, "_switch_logs_provider",
                        lambda self: setattr(self, "_logs_w3", type("W3", (), {"eth": public})()) or True)
    out, hi = c.logs({"fromBlock": 1, "toBlock": 3000}, chunk=2000, pause=0)
    assert len(primary.calls) == 1                    # refused once, never asked again
    assert public.calls == [(1, 2000), (2001, 3000)]
    assert hi == 3000 and len(out) == 2
    assert c.logs_eth() is public and c.w3.eth is primary


def test_without_a_dedicated_endpoint_there_is_no_fallback(monkeypatch):
    monkeypatch.delenv("STONKFLYRH_RPC_URL", raising=False)
    monkeypatch.delenv("STONKFLYRH_LOGS_RPC_URL", raising=False)
    c = client(Eth())
    assert c._switch_logs_provider() is False
