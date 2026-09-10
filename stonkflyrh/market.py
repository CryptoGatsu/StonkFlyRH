"""Public Robinhood Chain observations. Fixtures are explicit test inputs.

There is no order book on a DEX, so bid and ask are derived the way a trader
actually experiences them: the quoter is asked what a probe of the run's own
order size would really receive in each direction. The gap between the two is
the round-trip cost of the pool at that size — pool fee plus price impact — and
the risk guard rejects a pool whose round trip is too wide to trade.
"""

import math
import time
from dataclasses import asdict, dataclass
from decimal import Decimal

from .chain import QUOTER_ABI, checksum
from .config import D, QUOTE_DECIMALS, from_wei, to_wei

POOL_ABI = [
    {
        "name": "token0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "token1",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "observe",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "secondsAgos", "type": "uint32[]"}],
        "outputs": [
            {"name": "tickCumulatives", "type": "int56[]"},
            {"name": "secondsPerLiquidityCumulativeX128", "type": "uint160[]"},
        ],
    },
]


@dataclass(frozen=True)
class Quote:
    """WETH per whole token, at the size this run would actually trade."""

    product: str
    bid: Decimal
    ask: Decimal
    timestamp: float
    base_decimals: int
    quote_decimals: int
    pool_fee: int
    probe_quote: Decimal
    probe_base: Decimal

    def __post_init__(self):
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or not 0 < self.bid <= self.ask
            or not math.isfinite(self.timestamp)
        ):
            raise ValueError("Invalid quote")
        if not 0 <= self.base_decimals <= 36 or self.quote_decimals != QUOTE_DECIMALS:
            raise ValueError("Invalid token decimals")
        if self.probe_quote <= 0 or self.probe_base <= 0:
            raise ValueError("Quote probe must be positive")

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def round_trip(self):
        return (self.ask - self.bid) / self.bid

    def json(self):
        return {
            k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()
        }


def tick_to_price(tick, base_decimals, base_is_token0):
    """Uniswap tick to WETH per whole memecoin."""
    raw = D(str(1.0001**tick))
    if base_is_token0:
        return raw * (D(10) ** (base_decimals - QUOTE_DECIMALS))
    other = raw * (D(10) ** (QUOTE_DECIMALS - base_decimals))
    if other <= 0:
        raise ValueError("Degenerate pool price")
    return 1 / other


class RobinhoodChainMarket:
    """Quotes read from the chain's own quoter contract. Read-only, no key."""

    HISTORY = 120

    def __init__(self, settings, client, registry, verified):
        self.s = settings
        self.client = client
        self.registry = registry
        self.verified = verified
        self.products = settings.products
        self.quoter = client.contract(registry.quoter, QUOTER_ABI)
        self.history = {p: [] for p in self.products}
        self.seeded = {}

    def _quote_call(self, token_in, token_out, amount_in, fee):
        params = (
            checksum(token_in),
            checksum(token_out),
            int(amount_in),
            int(fee),
            0,
        )
        # QuoterV2 reverts internally to report results, so it is always an
        # eth_call and never a transaction, whatever its declared mutability.
        result = self.quoter.functions.quoteExactInputSingle(params).call()
        amount_out = int(result[0] if isinstance(result, (list, tuple)) else result)
        if amount_out <= 0:
            raise RuntimeError("Quoter returned no output; pool is empty or unroutable")
        return amount_out

    def _seed(self, product, pool_address, base_decimals, mid):
        """Seed the chart from the pool's TWAP oracle when it has the history."""
        pool = self.client.contract(pool_address, POOL_ABI)
        try:
            token0 = checksum(pool.functions.token0().call())
            base_is_token0 = token0 == checksum(
                self.registry.token(product)["address"]
            )
            step = 60
            secondsAgos = [step * i for i in range(self.HISTORY, -1, -1)]
            cumulatives = pool.functions.observe(secondsAgos).call()[0]
            series = []
            for older, newer in zip(cumulatives, cumulatives[1:]):
                avg_tick = int((int(newer) - int(older)) / step)
                series.append(float(tick_to_price(avg_tick, base_decimals, base_is_token0)))
            if not series or any(not math.isfinite(v) or v <= 0 for v in series):
                raise RuntimeError("Oracle produced an unusable series")
            self.seeded[product] = "pool-twap"
            return series
        except Exception:
            # A young pool has one observation slot. A flat seed is honest: the
            # chart fills in from live observations rather than invented candles.
            self.seeded[product] = "flat-from-first-observation"
            return [float(mid)] * self.HISTORY

    def snapshot(self):
        result = {}
        now = time.time()
        for product in self.products:
            entry = self.registry.token(product)
            fee = self.registry.pool_fee(product, self.s.pool_fee_tier)
            base_decimals = entry["decimals"]
            quote_token = self.registry.quote_address
            probe_quote_wei = to_wei(self.s.order_limit, QUOTE_DECIMALS)
            if probe_quote_wei <= 0:
                raise RuntimeError("Order limit rounds to zero quote units")
            # Buy leg: what this run's own order size actually receives.
            base_out = self._quote_call(
                quote_token, entry["address"], probe_quote_wei, fee
            )
            # Sell leg: what that same quantity fetches back on the way out.
            quote_back = self._quote_call(
                entry["address"], quote_token, base_out, fee
            )
            probe_base = from_wei(base_out, base_decimals)
            ask = from_wei(probe_quote_wei, QUOTE_DECIMALS) / probe_base
            bid = from_wei(quote_back, QUOTE_DECIMALS) / probe_base
            if bid > ask:
                raise RuntimeError("Sell leg exceeded buy leg; quoter is inconsistent")
            quote = Quote(
                product,
                bid,
                ask,
                now,
                base_decimals,
                QUOTE_DECIMALS,
                fee,
                from_wei(probe_quote_wei, QUOTE_DECIMALS),
                probe_base,
            )
            if not self.history[product]:
                self.history[product] = self._seed(
                    product,
                    self.verified["pools"][product]["pool"],
                    base_decimals,
                    quote.mid,
                )
            result[product] = quote
        return result

    def record(self, quotes):
        for p, q in quotes.items():
            self.history[p].append(float(q.mid))
            self.history[p] = self.history[p][-self.HISTORY :]

    def report(self):
        return {"feed": "robinhood-chain-quoter", "seed": dict(self.seeded)}


class FixtureMarket:
    """Deterministic offline prices for verification. Never used in live mode."""

    HISTORY = 120

    def __init__(self, settings, *_):
        self.s = settings
        self.products = settings.products
        self.tick = 0
        self.history = {p: [] for p in self.products}

    def snapshot(self):
        quotes = {}
        now = time.time()
        for j, p in enumerate(self.products):
            base = D("0.0000001") * (D(10) ** (j % 3))
            price = base * D(1 + 0.06 * math.sin(self.tick * 0.35 + j))
            half = D(self.s.paper_pool_fee)
            quotes[p] = Quote(
                p,
                price * (1 - half),
                price * (1 + half),
                now,
                18,
                QUOTE_DECIMALS,
                int(self.s.pool_fee_tier),
                D(self.s.order_limit),
                D(self.s.order_limit) / (price * (1 + half)),
            )
            if not self.history[p]:
                self.history[p] = [
                    float(base * D(1 + 0.03 * math.sin(i * 0.3 + j)))
                    for i in range(self.HISTORY)
                ]
        self.tick += 1
        return quotes

    record = RobinhoodChainMarket.record

    def report(self):
        return {"feed": "fixture", "seed": {p: "synthetic" for p in self.products}}
