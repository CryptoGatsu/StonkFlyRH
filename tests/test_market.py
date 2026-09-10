"""Quoting is exercised against an in-memory quoter. No RPC call is made."""

import numpy as np
import pytest

from stonkflyrh.config import D, QUOTE_DECIMALS, Settings, to_wei
from stonkflyrh.display import market_frame, tick_label
from stonkflyrh.market import FixtureMarket, Quote, RobinhoodChainMarket, tick_to_price

PROBE = D("10")
WETH = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
POOL = "0x" + "33" * 20


class Call:
    def __init__(self, value):
        self.value = value

    def call(self, *_args, **_kwargs):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Functions:
    def __init__(self, owner):
        self.owner = owner

    def quoteExactInputSingle(self, params):
        return Call(self.owner.quote(params))

    def token0(self):
        return Call(self.owner.token0)

    def observe(self, seconds_agos):
        return Call(self.owner.observe(seconds_agos))


class Contract:
    def __init__(self, owner):
        self.functions = Functions(owner)


class FakeChain:
    """A constant-price pool: one unit of USDG always buys `rate` tokens."""

    net = type("Net", (), {"name": "Robinhood Chain", "chain_id": 4663})()

    def __init__(self, rate=D("4000"), pool_fee=D("0.01"), token0=TOKEN, observations=None):
        self.rate = rate
        self.pool_fee = pool_fee
        self.token0 = token0
        self.observations = observations

    def contract(self, address, abi):
        return Contract(self)

    def erc20(self, address):
        return Contract(self)

    def quote(self, params):
        token_in, token_out, amount_in, _fee, _limit = params
        net = D(amount_in) * (1 - self.pool_fee)
        scale = D(10) ** 12  # 6-decimal USDG against an 18-decimal memecoin
        if token_in.lower() == WETH.lower():
            return [int(net * self.rate * scale), 0, 0, 0]
        return [int(net / self.rate / scale), 0, 0, 0]

    def observe(self, seconds_agos):
        if self.observations is None:
            raise RuntimeError("OLD")
        return [self.observations, [0] * len(seconds_agos)]


class FakeRegistry:
    quote_address = WETH
    quote_decimals = 6
    quoter = "0x" + "44" * 20
    router = "0x" + "55" * 20

    def token(self, symbol):
        return {"symbol": symbol, "address": TOKEN, "decimals": 18}

    def pool_fee(self, symbol, default):
        return default


def build(chain=None, settings=None):
    settings = settings or Settings()
    verified = {"pools": {p: {"pool": POOL, "fee": 10000} for p in settings.products}}
    return RobinhoodChainMarket(settings, chain or FakeChain(), FakeRegistry(), verified)


# -- quote construction ----------------------------------------------------


def test_quote_rejects_a_crossed_book():
    with pytest.raises(ValueError):
        Quote("PONS", D("2"), D("1"), 0.0, 18, QUOTE_DECIMALS, 10000, D("1"), D("1"))


@pytest.mark.parametrize("bad", [D("0"), D("-1")])
def test_quote_rejects_nonpositive_prices(bad):
    with pytest.raises(ValueError):
        Quote("PONS", bad, D("1"), 0.0, 18, QUOTE_DECIMALS, 10000, D("1"), D("1"))


def test_quote_rejects_implausible_quote_decimals():
    with pytest.raises(ValueError):
        Quote("PONS", D("1"), D("1"), 0.0, 18, 99, 10000, D("1"), D("1"))


# -- on-chain quoting ------------------------------------------------------


def test_bid_and_ask_come_from_both_legs_at_the_order_size():
    market = build()
    q = market.snapshot(PROBE)["PONS"]
    assert q.bid < q.ask
    # A 1% pool taken twice: the round trip is about two pool fees wide.
    assert D("0.019") < q.round_trip < D("0.021")
    assert q.probe_quote == PROBE


def test_probe_size_is_the_one_the_caller_asked_for():
    market = build()
    q = market.snapshot(D("0.002"))["PONS"]
    assert q.probe_quote == D("0.002")
    assert q.pool_fee == 10000


def test_an_empty_pool_is_an_error_not_a_zero_price():
    class Empty(FakeChain):
        def quote(self, params):
            return [0, 0, 0, 0]

    with pytest.raises(RuntimeError, match="no output"):
        build(Empty()).snapshot(PROBE)


def test_history_seeds_flat_when_the_oracle_has_no_observations():
    market = build()
    q = market.snapshot(PROBE)["PONS"]
    assert market.seeded["PONS"] == "flat-from-first-observation"
    assert len(market.history["PONS"]) == RobinhoodChainMarket.HISTORY
    assert market.history["PONS"][0] == pytest.approx(float(q.mid))


def test_history_seeds_from_the_pool_oracle_when_available():
    # A cumulative tick series with a constant 60-second slope of tick 0.
    observations = [0] * (RobinhoodChainMarket.HISTORY + 1)
    market = build(FakeChain(observations=observations))
    market.snapshot(PROBE)
    assert market.seeded["PONS"] == "pool-twap"
    assert len(market.history["PONS"]) == RobinhoodChainMarket.HISTORY


def test_record_keeps_a_bounded_history():
    market = build()
    for _ in range(5):
        market.record(market.snapshot(PROBE))
    assert len(market.history["PONS"]) == RobinhoodChainMarket.HISTORY
    assert market.report()["feed"] == "robinhood-chain-quoter"


def test_tick_to_price_inverts_with_token_order():
    a = tick_to_price(0, 18, base_is_token0=True, quote_decimals=18)
    b = tick_to_price(0, 18, base_is_token0=False, quote_decimals=18)
    assert a == pytest.approx(float(b))
    assert tick_to_price(10000, 18, True) > tick_to_price(0, 18, True)
    assert tick_to_price(10000, 18, False) < tick_to_price(0, 18, False)


def test_tick_to_price_scales_with_decimals():
    six = tick_to_price(0, 6, base_is_token0=True)
    eighteen = tick_to_price(0, 18, base_is_token0=True)
    assert six < eighteen
    # An 18-decimal memecoin as token0 against 6-decimal USDG at tick 0.
    assert tick_to_price(0, 18, True, 6) == D(10) ** 12


# -- fixtures --------------------------------------------------------------


def test_fixture_market_is_offline_and_ordered():
    market = FixtureMarket(Settings(products=("PONS", "SHIB")))
    quotes = market.snapshot(PROBE)
    assert set(quotes) == {"PONS", "SHIB"}
    for q in quotes.values():
        assert 0 < q.bid < q.ask
    assert market.report()["feed"] == "fixture"


def test_fixture_prices_move_between_ticks():
    market = FixtureMarket(Settings())
    first = market.snapshot(PROBE)["PONS"].mid
    second = market.snapshot(PROBE)["PONS"].mid
    assert first != second


def test_fixture_market_refuses_to_price_on_chain():
    with pytest.raises(RuntimeError, match="does not price on chain"):
        FixtureMarket(Settings()).quote_call(WETH, TOKEN, 1, 10000)


# -- rendering -------------------------------------------------------------


def test_frame_is_a_fixed_rgb_chart():
    frame = market_frame("PONS", [1e-7 + i * 1e-10 for i in range(100)], D("1E-7"), D("1.01E-7"))
    assert frame.shape == (180, 320, 3)
    assert frame.dtype == np.uint8


def test_frame_tolerates_a_short_history():
    frame = market_frame("PONS", [1e-7], D("1E-7"), D("1.01E-7"))
    assert frame.shape == (180, 320, 3)


@pytest.mark.parametrize(
    "value,expected",
    [("0.0000001234567", "0.0000001234"), ("0", "0"), ("1.23456789", "1.234")],
)
def test_tick_label_keeps_four_significant_figures(value, expected):
    assert tick_label(D(value)) == expected


def test_market_exposes_its_pool_for_screening():
    settings = Settings()
    market = build(settings=settings)
    assert market.pool("PONS") == POOL


def test_to_wei_truncates_rather_than_rounds_up():
    assert to_wei(D("1.9999999999999999999"), 18) == 1999999999999999999
    assert to_wei(D("0.000000000000000001"), 18) == 1
