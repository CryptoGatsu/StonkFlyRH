"""The ETH/USD reference that values gas in the dollar ledger."""

import time

import pytest

from stonkflyrh.config import D, to_wei
from stonkflyrh.pricing import (
    FixtureOracle,
    StablePoolOracle,
    UsdOracle,
    build,
    check_plausible,
    gas_to_usd,
)

FEED = "0x" + "77" * 20
WETH = "0x" + "88" * 20
USDG = "0x" + "11" * 20


class Result:
    def __init__(self, value):
        self.value = value

    def call(self, *_a, **_k):
        return self.value


class FeedFunctions:
    def __init__(self, chain):
        self.chain = chain

    def decimals(self):
        return Result(self.chain.decimals)

    def description(self):
        return Result("ETH / USD")

    def latestRoundData(self):
        return Result((1, self.chain.answer, self.chain.updated_at, self.chain.updated_at, 1))


class FakeClient:
    def __init__(self, answer=2500 * 10**8, decimals=8, updated_at=None, code=True):
        self.answer = answer
        self.decimals = decimals
        self.updated_at = int(updated_at if updated_at is not None else time.time())
        self.code = code

    def require_code(self, address, label):
        if not self.code:
            raise RuntimeError(f"{label} holds no code")
        return address

    def contract(self, _address, _abi):
        return type("C", (), {"functions": FeedFunctions(self)})()

    def try_call(self, _address, _abi, function, *_a):
        return "ETH / USD" if function == "description" else None


class FakeMarket:
    registry = type("R", (), {"quote_address": USDG, "quote_decimals": 6})()

    def __init__(self, out):
        self.out = out
        self.calls = []

    def quote_call(self, token_in, token_out, amount_in, fee):
        self.calls.append((token_in, token_out, amount_in, fee))
        return self.out


# -- gas valuation ---------------------------------------------------------


def test_gas_converts_to_dollars():
    assert gas_to_usd(10**15, D("2500")) == D("2.5")  # 0.001 ETH


@pytest.mark.parametrize("price", ["49", "100001", "0"])
def test_an_implausible_price_is_refused(price):
    with pytest.raises(RuntimeError, match="plausible band"):
        check_plausible(D(price))


# -- Chainlink -------------------------------------------------------------


def test_a_fresh_feed_prices_in_dollars():
    oracle = UsdOracle(FakeClient(), FEED)
    assert oracle.eth_usd() == D("2500")
    assert oracle.report()["source"] == "chainlink"


def test_a_stale_feed_stops_the_run():
    with pytest.raises(RuntimeError, match="stale"):
        UsdOracle(FakeClient(updated_at=time.time() - 90000), FEED).eth_usd()


def test_a_feed_ahead_of_us_stops_the_run():
    with pytest.raises(RuntimeError, match="stale or ahead"):
        UsdOracle(FakeClient(updated_at=time.time() + 600), FEED).eth_usd()


def test_a_non_positive_answer_stops_the_run():
    with pytest.raises(RuntimeError, match="non-positive"):
        UsdOracle(FakeClient(answer=0), FEED).eth_usd()


def test_an_absurd_answer_stops_the_run():
    with pytest.raises(RuntimeError, match="plausible band"):
        UsdOracle(FakeClient(answer=5 * 10**8 * 10**6), FEED).eth_usd()


def test_a_feed_address_with_no_code_is_refused():
    with pytest.raises(RuntimeError, match="holds no code"):
        UsdOracle(FakeClient(code=False), FEED)


def test_feed_decimals_are_honoured():
    assert UsdOracle(FakeClient(answer=2500 * 10**18, decimals=18), FEED).eth_usd() == D("2500")


# -- the WETH/USDG pool ----------------------------------------------------


def test_the_pool_prices_eth_from_a_real_swap_quote():
    # 0.01 WETH in, 25 USDG out: $2500 an ETH.
    market = FakeMarket(to_wei("25", 6))
    oracle = StablePoolOracle(market, WETH, 6, pool_fee=500)
    assert oracle.eth_usd() == D("2500")
    assert market.calls[0][:2] == (WETH, USDG)
    assert market.calls[0][3] == 500
    assert oracle.report()["source"] == "weth-usdg-pool"


def test_an_empty_pool_is_refused():
    with pytest.raises(RuntimeError, match="plausible band"):
        StablePoolOracle(FakeMarket(1), WETH, 6).eth_usd()


# -- selection -------------------------------------------------------------


def test_fixture_runs_price_offline():
    oracle = build(None, None, None, fixture=True)
    assert isinstance(oracle, FixtureOracle)
    assert oracle.eth_usd() == D("2500")


def test_chainlink_wins_when_both_are_configured():
    registry = type("R", (), {"eth_usd_feed": FEED, "weth": WETH, "quote_decimals": 6})()
    assert build(FakeClient(), registry, FakeMarket(0)).source == "chainlink"


def test_the_weth_pool_is_the_default():
    registry = type(
        "R", (), {"eth_usd_feed": None, "weth": WETH, "weth_pool_fee": 500, "quote_decimals": 6}
    )()
    oracle = build(FakeClient(), registry, FakeMarket(to_wei("25", 6)))
    assert oracle.source == "weth-usdg-pool"
    assert oracle.eth_usd() == D("2500")


def test_a_registry_with_no_reference_refuses_to_value_gas():
    registry = type("R", (), {"eth_usd_feed": None, "weth": None})()
    with pytest.raises(RuntimeError, match="cannot be valued"):
        build(FakeClient(), registry, FakeMarket(0))
