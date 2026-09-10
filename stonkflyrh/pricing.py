"""The ETH/USD reference that turns dollar limits into WETH amounts.

Limits are configured in dollars because that is how a person thinks about
risk: a $10 order should stay a $10 order when ETH moves 30%. The ledger stays
denominated in WETH, which is what the wallet actually holds; only the limits
are converted, and they are reconverted every observation.

Robinhood Chain carries Chainlink feeds, so that is the primary source. A run
can instead price against a stablecoin pool through the same quoter it trades
with. Either way a stale, zero or absurd answer stops the run rather than
silently resizing every order.
"""

import time

from .chain import CHAINLINK_ABI, checksum
from .config import D, QUOTE_DECIMALS, from_wei, to_wei

# An ETH price outside this band means the feed is wrong, not that the market
# moved. Sizing every order off a bad number is worse than not trading.
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
        price = D(int(answer)) / (D(10) ** self.decimals)
        return check_plausible(price)

    def report(self):
        return {
            "source": self.source,
            "feed": self.feed,
            "description": self.description,
            "decimals": self.decimals,
        }


class StablePoolOracle:
    """ETH/USD from selling a probe of WETH into a stablecoin pool.

    The fallback for a run with no feed configured. It is a real execution
    price rather than an index, which is the right number for sizing an order
    that will be executed, but it moves with that one pool's depth.
    """

    source = "stable-pool"

    def __init__(self, market, stable):
        self.market = market
        self.stable = dict(stable)
        self.address = checksum(stable["address"])
        self.decimals = int(stable["decimals"])
        self.fee = int(stable.get("pool_fee", 500))
        self.probe = D(stable.get("probe_weth", "0.01"))

    def eth_usd(self, now=None):
        probe_wei = to_wei(self.probe, QUOTE_DECIMALS)
        out = self.market.quote_call(
            self.market.registry.quote_address, self.address, probe_wei, self.fee
        )
        price = from_wei(out, self.decimals) / self.probe
        return check_plausible(price)

    def report(self):
        return {
            "source": self.source,
            "stable": self.stable["symbol"],
            "address": self.address,
            "pool_fee": self.fee,
        }


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


def usd_to_weth(usd, eth_usd):
    """Dollars to WETH at the current reference."""
    eth_usd = D(eth_usd)
    if eth_usd <= 0:
        raise ValueError("Non-positive ETH price")
    return D(usd) / eth_usd


def weth_to_usd(weth, eth_usd):
    return D(weth) * D(eth_usd)


def build(client, registry, market=None, fixture=False):
    """Pick the reference this run will size against."""
    if fixture:
        return FixtureOracle()
    feed = getattr(registry, "eth_usd_feed", None)
    if feed:
        return UsdOracle(client, feed)
    stable = getattr(registry, "stable", None)
    if stable and market is not None:
        return StablePoolOracle(market, stable)
    raise RuntimeError(
        "No ETH/USD reference. Add 'eth_usd_feed' (a Chainlink aggregator) or "
        "'stable' to the registry: dollar limits cannot be sized without one."
    )
