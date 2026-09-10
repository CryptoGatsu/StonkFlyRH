import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

# Robinhood Chain memecoins trade against wrapped ETH, so every amount in this
# configuration is denominated in WETH rather than in dollars.
QUOTE_SYMBOL = "WETH"
QUOTE_DECIMALS = 18

# Blast-radius caps. Upstream limited a run to $100 of capital and $10 an order;
# these are the same idea in the quote asset of this chain.
MAX_CAPITAL = Decimal("1")
MAX_ORDER = Decimal("0.1")

# Uniswap v3 fee tiers, in hundredths of a basis point. Memecoin pools are
# usually 1%; a tier with no pool is rejected at preflight.
POOL_FEE_TIERS = (500, 3000, 10000)

SYMBOL = re.compile(r"^[A-Z0-9]{2,12}$")


def D(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not money")
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError("Nonfinite quantity")
    return x


def down(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_DOWN) * D(step)


def up(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_UP) * D(step)


def to_wei(amount, decimals=QUOTE_DECIMALS):
    """Decimal token amount to integer base units, truncating sub-unit dust."""
    return int((D(amount) * (D(10) ** int(decimals))).to_integral_value(rounding=ROUND_DOWN))


def from_wei(amount, decimals=QUOTE_DECIMALS):
    return D(int(amount)) / (D(10) ** int(decimals))


@dataclass(frozen=True)
class Settings:
    """Run parameters. Products are memecoin symbols traded against WETH."""

    network: str = "robinhood-mainnet"
    products: tuple[str, ...] = ("DOGE",)
    capital: str = "0.05"
    order_limit: str = "0.005"
    min_order_quote: str = "0.0005"
    loss_stop: str = "0.01"
    protocol_fee_bps: int = 100
    pool_fee_tier: int = 10000
    gas_reserve: str = "0.002"
    slippage: str = "0.01"
    spread_limit: str = "0.03"
    max_gas_price_gwei: str = "5"
    max_gas_share: str = "0.25"
    paper_gas_price_gwei: str = "0.05"
    gas_limit: int = 500000
    daily_orders: int = 24
    interval_seconds: float = 60
    max_quote_age: float = 30
    neural_ms: float = 500
    neural_bin_ms: float = 10
    pulse_ms: float = 200
    pulse_current: float = 20
    reward_deadband: str = "0.00002"
    decoder_threshold_hz: float = 2
    paper_pool_fee: str = "0.01"
    learning: bool = True

    def __post_init__(self):
        from .chain import NETWORKS
        from .fees import BPS_DENOMINATOR

        if self.network not in NETWORKS:
            raise ValueError("Unknown network")
        if (
            not self.products
            or len(set(self.products)) != len(self.products)
            or not all(isinstance(p, str) and SYMBOL.match(p) for p in self.products)
            or QUOTE_SYMBOL in self.products
        ):
            raise ValueError("Products must be distinct memecoin symbols, not the quote asset")
        if len(self.products) > 8:
            raise ValueError("At most 8 products per run")
        if not 0 < D(self.capital) <= MAX_CAPITAL:
            raise ValueError(f"Maximum capital {MAX_CAPITAL} {QUOTE_SYMBOL}")
        if not 0 < D(self.order_limit) <= min(D(self.capital), MAX_ORDER):
            raise ValueError(f"Maximum order {MAX_ORDER} {QUOTE_SYMBOL}")
        if not 0 < D(self.loss_stop) <= D(self.capital):
            raise ValueError("Invalid loss stop")
        if not 0 < D(self.min_order_quote) <= D(self.order_limit):
            raise ValueError("Minimum order must be positive and within the order limit")
        if (
            type(self.protocol_fee_bps) is not int
            or not 0 <= self.protocol_fee_bps <= 300
        ):
            raise ValueError("Protocol fee must be 0-300 bps")
        if self.pool_fee_tier not in POOL_FEE_TIERS:
            raise ValueError("Unsupported Uniswap v3 fee tier")
        if not D(0) < D(self.gas_reserve) <= D(self.capital) / 2:
            raise ValueError("Gas reserve must be positive and well under capital")
        if not D(0) <= D(self.slippage) <= D("0.05"):
            raise ValueError("Slippage tolerance must be 0-5%")
        if not D(0) < D(self.spread_limit) <= D("0.10"):
            raise ValueError("Round-trip impact limit must be 0-10%")
        if not D(0) < D(self.max_gas_price_gwei) <= D(1000):
            raise ValueError("Invalid gas price ceiling")
        if not D(0) < D(self.max_gas_share) <= D("0.5"):
            raise ValueError("Gas may take at most half of an order notional")
        if not D(0) < D(self.paper_gas_price_gwei) <= D(self.max_gas_price_gwei):
            raise ValueError("Simulated gas price must sit under the live ceiling")
        if type(self.gas_limit) is not int or not 100000 <= self.gas_limit <= 3000000:
            raise ValueError("Invalid gas limit")
        if not D(0) <= D(self.paper_pool_fee) <= D("0.05"):
            raise ValueError("Invalid simulated pool fee")
        if (
            type(self.daily_orders) is not int
            or not 1 <= self.daily_orders <= 100
            or not math.isfinite(self.interval_seconds)
            or self.interval_seconds < 60
        ):
            raise ValueError("Rate limit: >=60 s between orders, <=100 orders/day")
        if D(self.reward_deadband) <= 0:
            raise ValueError("Positive reinforcement deadband required")
        if self.protocol_fee_bps > BPS_DENOMINATOR:
            raise ValueError("Protocol fee exceeds 100%")
        for x in [
            self.max_quote_age,
            self.neural_ms,
            self.neural_bin_ms,
            self.pulse_ms,
            self.pulse_current,
            self.decoder_threshold_hz,
        ]:
            if not math.isfinite(x) or x <= 0:
                raise ValueError("Positive finite parameter required")
        if self.neural_bin_ms > 10 or self.pulse_ms > self.neural_ms:
            raise ValueError("Use <=10 ms neural bins; pulse must fit a decision window")
        if any(
            abs(x * 10 - round(x * 10)) > 1e-7
            for x in [self.neural_ms, self.neural_bin_ms, self.pulse_ms]
        ):
            raise ValueError("Neural intervals must be multiples of 0.1 ms")

    def signature(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()
