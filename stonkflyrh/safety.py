"""Rug screening, and what the run does after it is rugged anyway.

Two halves.

**Before a buy**, `RugScreen` asks the chain a set of questions about the token
and its pool and returns a verdict. Every question is answered from chain state
this process reads itself: no reputation service, no allowlist, no scores from
somewhere else. The screen can only ever *withhold* a buy. It never proposes
one, never picks a different token, and never overrides a neural HOLD. That
asymmetry is the point: a screen that is too strict costs missed trades, while
a screen that can act costs money.

**After a buy**, `RugWatch` keeps looking. A position whose bid collapses past
the configured drawdown, or that stops being sellable at all, is recorded as a
rug: the token goes on a permanent blocklist, and the observation loop delivers
a longer aversive pulse to the identified PPL101 cells than an ordinary loss
does. The thresholds the screen uses also tighten each time, so the next token
has to clear a higher bar than the one that just failed.

The tightening is an engineered heuristic over recorded features, disclosed as
such. It is not the connectome learning, and nothing here edits a synapse.
"""

import time
from dataclasses import dataclass, field

from .chain import ERC20_ABI, OWNABLE_ABI, ZERO_ADDRESS, checksum
from .config import D, from_wei, to_wei

POOL_STATE_ABI = [
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
    },
    {
        "name": "liquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}],
    },
]

# Functions whose presence means the token's author kept a lever after launch.
# Grouped by what the lever does to a holder who already bought.
DANGEROUS_SIGNATURES = {
    "supply": [
        "mint(address,uint256)",
        "mint(uint256)",
        "mintTo(address,uint256)",
        "issue(uint256)",
    ],
    "freeze": [
        "blacklist(address,bool)",
        "setBlacklist(address,bool)",
        "addBlacklist(address)",
        "setBots(address[],bool)",
        "pause()",
        "setTradingEnabled(bool)",
        "enableTrading()",
        "setMaxTxAmount(uint256)",
        "setMaxWallet(uint256)",
    ],
    "tax": [
        "setFee(uint256)",
        "setFees(uint256,uint256)",
        "setTaxes(uint256,uint256)",
        "setBuyTax(uint256)",
        "setSellTax(uint256)",
    ],
}


def selector(signature):
    from eth_utils import keccak

    return keccak(text=signature)[:4].hex()


def dangerous_selectors():
    return {
        category: {selector(sig): sig for sig in signatures}
        for category, signatures in DANGEROUS_SIGNATURES.items()
    }


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str
    value: object = None

    def json(self):
        value = self.value
        if isinstance(value, D("0").__class__):
            value = str(value)
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "value": value,
        }


@dataclass
class Verdict:
    product: str
    address: str
    checked_at: float
    checks: list = field(default_factory=list)
    error: str = None

    @property
    def approved(self):
        return self.error is None and all(c.passed for c in self.checks)

    def failures(self):
        return [c for c in self.checks if not c.passed]

    def reason(self):
        if self.error:
            return f"screen could not complete: {self.error}"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures())

    def json(self):
        return {
            "product": self.product,
            "address": self.address,
            "checked_at": self.checked_at,
            "approved": self.approved,
            "error": self.error,
            "checks": [c.json() for c in self.checks],
        }

    @classmethod
    def from_json(cls, blob):
        v = cls(blob["product"], blob["address"], blob["checked_at"], [], blob.get("error"))
        v.checks = [
            Check(c["name"], c["passed"], c["detail"], c.get("value")) for c in blob["checks"]
        ]
        return v


class RugScreen:
    """Chain-state questions asked before a buy is allowed."""

    # A probe this much smaller than the order carries almost no price impact,
    # so what it loses on a round trip is the pool fee plus any transfer tax.
    # Comparing it against a full-size probe separates the two.
    SMALL_PROBE_DIVISOR = 1000

    def __init__(self, settings, client, registry, ledger, quote_call):
        self.s = settings
        self.client = client
        self.registry = registry
        self.l = ledger
        self.quote_call = quote_call
        self.selectors = dangerous_selectors()

    # -- adaptive thresholds -------------------------------------------------

    def tightening(self):
        """Each recorded rug raises the bar for every token after it."""
        if not self.s.adapt_enabled:
            return D(1)
        rugs = len(self.l.rugs())
        return D(self.s.rug_tightening) ** min(rugs, 4)

    def min_liquidity_usd(self):
        return D(self.s.min_liquidity_usd) * self.tightening()

    def max_transfer_tax(self):
        return D(self.s.max_transfer_tax) / self.tightening()

    def max_price_impact(self):
        return D(self.s.max_price_impact) / self.tightening()

    def thresholds(self):
        return {
            "tightening": str(self.tightening()),
            "rugs_recorded": len(self.l.rugs()),
            "min_liquidity_usd": str(self.min_liquidity_usd()),
            "max_transfer_tax": str(self.max_transfer_tax()),
            "max_price_impact": str(self.max_price_impact()),
        }

    # -- the screen ----------------------------------------------------------

    def assess(self, product, pool, eth_usd, now=None, force=False):
        now = time.time() if now is None else now
        entry = self.registry.token(product)
        address = checksum(entry["address"])
        if not force:
            cached = self.l.screen_get(product, now, self.s.screen_ttl_seconds)
            if cached and cached["address"] == address:
                return Verdict.from_json(cached)
        verdict = Verdict(product, address, now)
        try:
            verdict.checks = self._run(product, entry, pool, eth_usd)
        except Exception as e:
            verdict.error = f"{type(e).__name__}: {e}"
        self.l.screen_put(product, verdict.json(), now)
        return verdict

    def _run(self, product, entry, pool, eth_usd):
        address = checksum(entry["address"])
        checks = [
            self._code(address),
            self._upgradeable(address),
            self._levers(address),
            self._ownership(address),
        ]
        checks += self._pool(product, entry, pool, eth_usd)
        return checks

    def _code(self, address):
        has_code = self.client.has_code(address)
        return Check(
            "contract_code",
            has_code,
            "token address holds code" if has_code else "no code at the token address",
            has_code,
        )

    def _upgradeable(self, address):
        upgradeable = self.client.is_upgradeable(address)
        if not self.s.reject_upgradeable:
            return Check(
                "upgradeable", True, "proxy check disabled by configuration", upgradeable
            )
        return Check(
            "upgradeable",
            not upgradeable,
            "implementation can be swapped (EIP-1967 proxy)"
            if upgradeable
            else "not an EIP-1967 proxy",
            upgradeable,
        )

    def _levers(self, address):
        found = self.client.selectors(address)
        hits = {}
        for category, table in self.selectors.items():
            matched = [sig for sel, sig in table.items() if sel in found]
            if matched:
                hits[category] = matched
        if not self.s.reject_dangerous_selectors:
            return Check("owner_levers", True, "selector scan disabled", hits or None)
        return Check(
            "owner_levers",
            not hits,
            "bytecode contains "
            + ", ".join(f"{k} ({', '.join(v)})" for k, v in hits.items())
            if hits
            else "no mint, freeze or tax selectors in bytecode",
            hits or None,
        )

    def _ownership(self, address):
        owner = self.client.try_call(address, OWNABLE_ABI, "owner")
        if owner is None:
            # No Ownable interface is the good case, not a missing answer.
            return Check("ownership", True, "no owner() function", None)
        owner = checksum(owner)
        renounced = owner == ZERO_ADDRESS
        return Check(
            "ownership",
            renounced,
            "ownership renounced" if renounced else f"owner is still {owner}",
            owner,
        )

    def _pool(self, product, entry, pool, eth_usd):
        checks = []
        pool = checksum(pool)
        quote_token = self.registry.quote_address
        fee = self.registry.pool_fee(product, self.s.pool_fee_tier)
        decimals = entry["decimals"]

        qd = self.registry.quote_decimals
        quote_in_pool = int(
            self.client.contract(quote_token, ERC20_ABI).functions.balanceOf(pool).call()
        )
        # Both sides of a pool are worth about the same, so twice the USDG leg
        # approximates its depth in dollars. Concentrated liquidity makes this a
        # floor, not an exact TVL, which is the safe direction for a minimum.
        liquidity_usd = from_wei(quote_in_pool, qd) * 2
        floor = self.min_liquidity_usd()
        checks.append(
            Check(
                "liquidity",
                liquidity_usd >= floor,
                f"pool holds about ${liquidity_usd:.0f}, floor ${floor:.0f}",
                str(liquidity_usd),
            )
        )

        observations = self.client.try_call(pool, POOL_STATE_ABI, "slot0")
        cardinality = int(observations[3]) if observations else 0
        checks.append(
            Check(
                "pool_history",
                cardinality >= self.s.min_pool_observations,
                f"pool keeps {cardinality} oracle observations, "
                f"floor {self.s.min_pool_observations}",
                cardinality,
            )
        )

        order_probe = to_wei(self.s.order_limit_usd, qd)
        small_probe = max(1, order_probe // self.SMALL_PROBE_DIVISOR)
        small = self._round_trip(entry["address"], quote_token, small_probe, fee, decimals)
        if small is None:
            checks.append(
                Check("sellable", False, "a token bought here could not be sold back", None)
            )
            return checks
        checks.append(Check("sellable", True, "sell leg quotes a non-zero return", True))

        pool_cost = D(fee) / D(1000000) * 2
        implied_tax = max(D(0), (small - pool_cost) / 2)
        tax_ceiling = self.max_transfer_tax()
        checks.append(
            Check(
                "transfer_tax",
                implied_tax <= tax_ceiling,
                f"about {implied_tax * 100:.2f}% per side beyond the pool fee, "
                f"ceiling {tax_ceiling * 100:.2f}%",
                str(implied_tax),
            )
        )

        big = self._round_trip(entry["address"], quote_token, order_probe, fee, decimals)
        if big is None:
            checks.append(
                Check("price_impact", False, "order-size probe does not round trip", None)
            )
            return checks
        impact = max(D(0), big - small)
        impact_ceiling = self.max_price_impact()
        checks.append(
            Check(
                "price_impact",
                impact <= impact_ceiling,
                f"{impact * 100:.2f}% at the run's order size, "
                f"ceiling {impact_ceiling * 100:.2f}%",
                str(impact),
            )
        )
        return checks

    def _round_trip(self, base, quote, probe_wei, fee, decimals):
        """Fraction lost buying and immediately selling `probe_wei` of USDG."""
        try:
            base_out = self.quote_call(quote, base, probe_wei, fee)
            quote_back = self.quote_call(base, quote, base_out, fee)
        except Exception:
            return None
        if base_out <= 0 or quote_back <= 0:
            return None
        return (D(probe_wei) - D(quote_back)) / D(probe_wei)

    def require(self, product, pool, eth_usd, now=None):
        """Raise if this token may not be bought. Never returns a substitute."""
        from .risk import Veto

        if self.l.is_blocked(product):
            raise Veto(f"{product} is blocklisted: {self.l.block_reason(product)}")
        if not self.s.screen_enabled:
            return None
        verdict = self.assess(product, pool, eth_usd, now)
        if not verdict.approved:
            raise Veto(f"rug screen rejected {product} — {verdict.reason()}")
        return verdict


class RugWatch:
    """Detects a rug after the fact and turns it into a durable consequence."""

    def __init__(self, settings, ledger, screen=None):
        self.s = settings
        self.l = ledger
        self.screen = screen

    def entry_price(self, product):
        recorded = self.l.get("entries") or {}
        return D(recorded[product]) if product in recorded else None

    def record_entry(self, product, price):
        recorded = dict(self.l.get("entries") or {})
        # The reference is the worst price paid while the position is open, so a
        # collapse is measured against what was actually risked.
        existing = recorded.get(product)
        recorded[product] = str(max(D(price), D(existing)) if existing else D(price))
        self.l.put("entries", recorded)

    def clear_entry(self, product):
        recorded = dict(self.l.get("entries") or {})
        recorded.pop(product, None)
        self.l.put("entries", recorded)

    def inspect(self, product, quote, now=None):
        """Return a rug record if an open position has gone bad, else None."""
        now = time.time() if now is None else now
        if self.l.is_blocked(product):
            # Already recorded. A rug is one event, however long the exit takes.
            return None
        held = self.l.positions.get(product, D(0))
        if held <= 0:
            return None
        entry = self.entry_price(product)
        if entry is None or entry <= 0:
            return None
        drop = (entry - quote.bid) / entry
        if drop < D(self.s.rug_drawdown):
            return None
        record = {
            "product": product,
            "at": now,
            "entry_price": str(entry),
            "exit_price": str(quote.bid),
            "drawdown": str(drop),
            "reason": f"bid fell {drop * 100:.1f}% below entry",
            "screen": self.l.screen_raw(product),
        }
        self.l.record_rug(record)
        return record

    def pulse_ms(self):
        """A rug is delivered as a longer aversive pulse than a normal loss."""
        return float(D(self.s.pulse_ms) * D(self.s.rug_pulse_multiplier))
