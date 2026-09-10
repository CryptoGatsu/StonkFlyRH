"""The ETH/USD reference, used to value gas in the run's dollar ledger.

Trades settle in USDG, so a $10 order is 10 USDG and needs no conversion. Gas
is native ETH, and equity should feel it, so the run needs a dollar figure for
ETH. The default source is the run's own quoter: sell a probe of WETH into the
USDG pool and read what comes back — an execution price, not an index, which is
the right number for what gas actually costs to replace. A Chainlink feed is
accepted when the registry names one. A stale, zero or absurd answer stops the
run rather than mis-valuing every observation's gas.
"""

import time

from .chain import CHAINLINK_ABI, checksum
from .config import D, GAS_DECIMALS, from_wei, to_wei

# An ETH price outside this band means the source is wrong, not that the market
# moved.
MIN_PLAUSIBLE_USD = D("50")
MAX_PLAUSIBLE_USD = D("100000")
MAX_FEED_AGE_SECONDS = 3600


class UsdOracle:
    """ETH/USD from a Chainlink aggregator."""

    source = "chainlink"

    def __init__(self, client, feed, max_age=MAX_FEED_AGE_SECONDS):
        self.client = client
        self.feed = client.require_code(feed, "ETH/USD feed")
        self.max_age = max_age
        self.contract = client.contract(self.feed, CHAINLINK_ABI)
        self.decimals = int(self.contract.functions.decimals().call())
        if not 0 < self.decimals <= 36:
            raise RuntimeError("Implausible price feed decimals")
        self.description = client.try_call(self.feed, CHAINLINK_ABI, "description")

    def eth_usd(self, now=None):
        now = time.time() if now is None else now
        _round_id, answer, _started, updated_at, _answered = (
            self.contract.functions.latestRoundData().call()
        )
        if int(answer) <= 0:
            raise RuntimeError("ETH/USD feed reported a non-positive price")
        age = now - int(updated_at)
        if not -60 <= age <= self.max_age:
            raise RuntimeError(f"ETH/USD feed is stale or ahead of us by {age:.0f}s")
        return check_plausible(D(int(answer)) / (D(10) ** self.decimals))

    def report(self):
        return {
            "source": self.source,
            "feed": self.feed,
            "description": self.description,
            "decimals": self.decimals,
        }


class StablePoolOracle:
    """ETH/USD from selling a probe of WETH into the USDG pool via the quoter."""

    source = "weth-usdg-pool"

    def __init__(self, market, weth, quote_decimals, pool_fee=500, probe_weth="0.01"):
        self.market = market
        self.weth = checksum(weth)
        self.quote_decimals = int(quote_decimals)
        self.fee = int(pool_fee)
        self.probe = D(probe_weth)

    def eth_usd(self, now=None):
        probe_wei = to_wei(self.probe, GAS_DECIMALS)
        out = self.market.quote_call(
            self.weth, self.market.registry.quote_address, probe_wei, self.fee
        )
        return check_plausible(from_wei(out, self.quote_decimals) / self.probe)

    def report(self):
        return {"source": self.source, "weth": self.weth, "pool_fee": self.fee}


class FixtureOracle:
    """A fixed price for offline runs. Never used against a live wallet."""

    source = "fixture"

    def __init__(self, price="2500"):
        self.price = D(price)

    def eth_usd(self, now=None):
        return self.price

    def report(self):
        return {"source": self.source, "eth_usd": str(self.price)}


def check_plausible(price):
    if not MIN_PLAUSIBLE_USD <= price <= MAX_PLAUSIBLE_USD:
        raise RuntimeError(f"ETH/USD of {price} is outside the plausible band")
    return price


def gas_to_usd(gas_wei, eth_usd):
    """Native gas spent, in dollars."""
    return from_wei(gas_wei, GAS_DECIMALS) * D(eth_usd)


def build(client, registry, market=None, fixture=False):
    """Pick the reference this run will value gas with."""
    if fixture:
        return FixtureOracle()
    feed = getattr(registry, "eth_usd_feed", None)
    if feed:
        return UsdOracle(client, feed)
    weth = getattr(registry, "weth", None)
    if weth and market is not None:
        return StablePoolOracle(
            market, weth, registry.quote_decimals, getattr(registry, "weth_pool_fee", 500)
        )
    raise RuntimeError(
        "No ETH/USD reference. Add 'weth' (priced through the USDG pool) or "
        "'eth_usd_feed' (a Chainlink aggregator) to the registry: gas cannot be "
        "valued without one."
    )
