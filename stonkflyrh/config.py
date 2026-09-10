import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

# Memecoins are traded against USDG, Robinhood Chain's settlement stablecoin, so
# the ledger is in dollars already: a $10 order is 10 USDG, with no conversion
# to drift. Gas is still ETH; the ETH/USD reference exists to value it.
QUOTE_SYMBOL = "USDG"
QUOTE_DECIMALS = 6
GAS_DECIMALS = 18

# Blast-radius caps. A run cannot be configured past these.
MAX_CAPITAL_USD = Decimal("1000")
MAX_ORDER_USD = Decimal("100")

# Uniswap v3 fee tiers, in hundredths of a basis point. Memecoin pools are
# usually 1%; a tier with no pool is rejected at preflight.
POOL_FEE_TIERS = (500, 3000, 10000)

SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9_]{1,15}$")


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
    products: tuple[str, ...] = ("PONS",)
    quote_symbol: str = QUOTE_SYMBOL
    quote_decimals: int = QUOTE_DECIMALS

    # -- size, in dollars ---------------------------------------------------
    capital_usd: str = "100"
    order_limit_usd: str = "10"
    min_order_usd: str = "1"
    loss_stop_usd: str = "25"

    # -- execution ----------------------------------------------------------
    protocol_fee_bps: int = 0
    pool_fee_tier: int = 10000
    slippage: str = "0.02"
    spread_limit: str = "0.06"
    max_gas_price_gwei: str = "5"
    max_gas_share: str = "0.25"
    paper_gas_price_gwei: str = "0.05"
    gas_limit: int = 500000
    daily_orders: int = 24
    interval_seconds: float = 60
    max_quote_age: float = 30

    # -- rug screen ---------------------------------------------------------
    screen_enabled: bool = True
    min_liquidity_usd: str = "25000"
    max_transfer_tax: str = "0.05"
    max_price_impact: str = "0.03"
    min_pool_observations: int = 4
    reject_upgradeable: bool = True
    reject_dangerous_selectors: bool = True
    screen_ttl_seconds: float = 900
    rug_drawdown: str = "0.5"
    rug_exit_spread: str = "0.5"

    # -- discovery ----------------------------------------------------------
    discovery_enabled: bool = True
    discovery_interval_seconds: float = 600
    discovery_lookback_blocks: int = 400000
    discovery_batch: int = 12
    max_products: int = 12

    # -- donations ----------------------------------------------------------
    donations_enabled: bool = False
    donor_share: str = "0.5"
    donation_min_usd: str = "1"
    donor_min_payout_usd: str = "1"
    donor_payout_interval_seconds: float = 3600
    max_pool_usd: str = "1000"
    loss_stop_fraction: str = "0.25"

    # -- adaptation ---------------------------------------------------------
    adapt_enabled: bool = True
    volatility_window: int = 30
    calm_volatility: str = "0.02"
    max_volatility: str = "0.25"
    min_size_scale: str = "0.25"
    loss_cooldown_multiplier: str = "3"
    rug_tightening: str = "1.5"

    # -- neural -------------------------------------------------------------
    neural_ms: float = 500
    neural_bin_ms: float = 10
    pulse_ms: float = 200
    pulse_current: float = 20
    rug_pulse_multiplier: str = "2"
    reward_deadband_usd: str = "0.05"
    decoder_threshold_hz: float = 2
    paper_pool_fee: str = "0.01"
    learning: bool = True

    def __post_init__(self):
        from .chain import NETWORKS

        if self.network not in NETWORKS:
            raise ValueError("Unknown network")
        if not NETWORKS[self.network].robinhood:
            raise ValueError("This fork trades Robinhood Chain only")
        if (
            len(set(self.products)) != len(self.products)
            or not all(isinstance(p, str) and SYMBOL.match(p) for p in self.products)
            or self.quote_symbol in self.products
        ):
            raise ValueError("Products must be distinct memecoin symbols, not the quote asset")
        if not self.products and not self.discovery_enabled:
            raise ValueError("With discovery off, at least one seed product is required")
        if not SYMBOL.match(self.quote_symbol) or not 0 <= self.quote_decimals <= 36:
            raise ValueError("Quote asset needs a symbol and plausible decimals")
        if len(self.products) > 8:
            raise ValueError("At most 8 seed products per run")
        if not 0 < D(self.capital_usd) <= MAX_CAPITAL_USD:
            raise ValueError(f"Maximum capital ${MAX_CAPITAL_USD}")
        if not 0 < D(self.order_limit_usd) <= min(D(self.capital_usd), MAX_ORDER_USD):
            raise ValueError(f"Maximum order ${MAX_ORDER_USD}, and within capital")
        if not 0 < D(self.min_order_usd) <= D(self.order_limit_usd):
            raise ValueError("Minimum order must be positive and within the order limit")
        if not 0 < D(self.loss_stop_usd) <= D(self.capital_usd):
            raise ValueError("Invalid loss stop")
        if type(self.protocol_fee_bps) is not int or not 0 <= self.protocol_fee_bps <= 300:
            raise ValueError("Protocol fee must be 0-300 bps")
        if self.pool_fee_tier not in POOL_FEE_TIERS:
            raise ValueError("Unsupported Uniswap v3 fee tier")
        if not D(0) <= D(self.slippage) <= D("0.10"):
            raise ValueError("Slippage tolerance must be 0-10%")
        if not D(0) < D(self.spread_limit) <= D("0.20"):
            raise ValueError("Round-trip impact limit must be 0-20%")
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
        self._check_screen()
        self._check_discovery()
        self._check_donations()
        self._check_adaptation()
        self._check_neural()

    def _check_donations(self):
        if type(self.donations_enabled) is not bool:
            raise ValueError("donations_enabled is a flag")
        if not D(0) <= D(self.donor_share) <= D(1):
            raise ValueError("Donor share must be a fraction between 0 and 1")
        if D(self.donation_min_usd) < 0 or D(self.donor_min_payout_usd) <= 0:
            raise ValueError("Donation and payout minimums must be sensible")
        if not math.isfinite(self.donor_payout_interval_seconds) or self.donor_payout_interval_seconds < 300:
            raise ValueError("Donor payouts run at most every five minutes")
        if not D(self.capital_usd) <= D(self.max_pool_usd) <= D("100000"):
            raise ValueError("Pool cap must hold the operator's stake and stay under $100,000")
        if not D(0) < D(self.loss_stop_fraction) < 1:
            raise ValueError("Loss stop fraction must be between 0 and 1")

    def _check_discovery(self):
        if type(self.discovery_enabled) is not bool:
            raise ValueError("discovery_enabled is a flag")
        if not math.isfinite(self.discovery_interval_seconds) or self.discovery_interval_seconds < 60:
            raise ValueError("Discovery runs at most once a minute")
        if type(self.discovery_lookback_blocks) is not int or not 0 <= self.discovery_lookback_blocks <= 5_000_000:
            raise ValueError("Discovery lookback must be 0-5,000,000 blocks")
        if type(self.discovery_batch) is not int or not 1 <= self.discovery_batch <= 50:
            raise ValueError("Discovery screens 1-50 candidates a scan")
        if type(self.max_products) is not int or not len(self.products) <= self.max_products <= 24:
            raise ValueError("max_products must hold the seeds and be at most 24")

    def _check_screen(self):
        if D(self.min_liquidity_usd) < 0:
            raise ValueError("Liquidity floor cannot be negative")
        if not D(0) <= D(self.max_transfer_tax) <= D("0.5"):
            raise ValueError("Transfer tax ceiling must be 0-50%")
        if not D(0) < D(self.max_price_impact) <= D("0.5"):
            raise ValueError("Price impact ceiling must be 0-50%")
        if type(self.min_pool_observations) is not int or self.min_pool_observations < 0:
            raise ValueError("Pool observation floor must be a non-negative integer")
        if type(self.reject_upgradeable) is not bool:
            raise ValueError("reject_upgradeable is a flag")
        if type(self.reject_dangerous_selectors) is not bool:
            raise ValueError("reject_dangerous_selectors is a flag")
        if not math.isfinite(self.screen_ttl_seconds) or self.screen_ttl_seconds <= 0:
            raise ValueError("Screen cache must expire")
        if not D(0) < D(self.rug_drawdown) < 1:
            raise ValueError("Rug drawdown must be a fraction between 0 and 1")
        if not D(self.spread_limit) <= D(self.rug_exit_spread) <= D("0.9"):
            raise ValueError("Rug exit spread must be at least the normal limit and under 90%")

    def _check_adaptation(self):
        if type(self.adapt_enabled) is not bool:
            raise ValueError("adapt_enabled is a flag")
        if type(self.volatility_window) is not int or not 5 <= self.volatility_window <= 120:
            raise ValueError("Volatility window must be 5-120 observations")
        if not D(0) < D(self.calm_volatility) < D(self.max_volatility) <= D(1):
            raise ValueError("Volatility band must be ordered and within 100%")
        if not D(0) < D(self.min_size_scale) <= D(1):
            raise ValueError("Size scale floor must be a fraction of the order limit")
        if not D(1) <= D(self.loss_cooldown_multiplier) <= D(20):
            raise ValueError("Loss cooldown multiplier must be 1-20")
        if not D(1) <= D(self.rug_tightening) <= D(5):
            raise ValueError("Rug tightening must be 1-5")

    def _check_neural(self):
        if D(self.reward_deadband_usd) <= 0:
            raise ValueError("Positive reinforcement deadband required")
        if not D(1) <= D(self.rug_pulse_multiplier) <= D(5):
            raise ValueError("Rug pulse multiplier must be 1-5")
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
        if D(self.rug_pulse_multiplier) * D(self.pulse_ms) > D(self.neural_ms):
            raise ValueError("A rug pulse must still fit inside a decision window")
        if any(
            abs(x * 10 - round(x * 10)) > 1e-7
            for x in [self.neural_ms, self.neural_bin_ms, self.pulse_ms]
        ):
            raise ValueError("Neural intervals must be multiples of 0.1 ms")

    def signature(self):
        # The seed list is not part of the protocol: discovery grows the universe
        # during a run, and changing the seeds on a restart adds to it.
        fields = {k: v for k, v in asdict(self).items() if k != "products"}
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
