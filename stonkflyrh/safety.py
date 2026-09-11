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

    @property
    def tradeable(self):
        """Approved for membership of the universe: everything about the token
        and its pool clears. The buy dry-run is judged separately, because a
        refusal there can be the run's own path failing rather than the token;
        it still vetoes the buy, and it shows on the screen, but it does not
        throw the token out."""
        return self.error is None and all(c.passed for c in self.checks if c.name != "executable")

    @property
    def retry(self):
        """Withheld for a reason that time fixes (an empty pool awaiting its
        liquidity), so the candidate should be looked at again, not remembered
        as rejected."""
        return any(c.name == "pool_empty" and not c.passed for c in self.checks)

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

    def __init__(self, settings, client, registry, ledger, quote_call, market=None):
        self.s = settings
        self.client = client
        self.registry = registry
        self.l = ledger
        self.quote_call = quote_call
        self.market = market
        self.selectors = dangerous_selectors()

    def _quote_call(self, product):
        if self.market is not None and hasattr(self.market, "quote_call_for"):
            return self.market.quote_call_for(product)
        return self.quote_call

    def _venue(self, product):
        if self.market is not None and hasattr(self.market, "venue_of"):
            return self.market.venue_of(product)
        return "v3"

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
            # A verdict from before a screen change is not this screen's verdict.
            if cached and cached["address"] == address and cached.get("screen_version") == self.SCREEN_VERSION:
                return Verdict.from_json(cached)
        verdict = Verdict(product, address, now)
        try:
            verdict.checks = self._run(product, entry, pool, eth_usd)
        except Exception as e:
            verdict.error = f"{type(e).__name__}: {e}"
        self.l.screen_put(product, {**verdict.json(), "screen_version": self.SCREEN_VERSION}, now)
        return verdict

    # Bump when a check is added or its meaning changes: cached verdicts from
    # an older screen are then re-run rather than trusted.
    SCREEN_VERSION = 3

    def _run(self, product, entry, pool, eth_usd):
        address = checksum(entry["address"])
        checks = [
            self._code(address),
            self._upgradeable(address),
            self._levers(address),
            self._ownership(address),
        ]
        checks += self._pool(product, entry, pool, eth_usd)
        checks += self._market(product, entry)
        if all(c.passed for c in checks):
            executable = self._executable(product, entry)
            if executable is not None:
                checks.append(executable)
        return checks

    # Set by the run when activity tracking is on; None means no such checks.
    activity = None

    def _market(self, product, entry):
        """Is anyone here? Recent swaps in the pool, and a market cap above the
        graveyard line (price × total supply, from the run's own quote)."""
        checks = []
        if self.activity is not None and self.s.activity_enabled:
            try:
                judged = self.activity.enough(product, entry, None)
            except Exception as e:
                judged = None
                checks.append(Check("activity", True, f"could not read swaps: {type(e).__name__}", None))
            if judged is not None:
                passed, detail, swaps = judged
                checks.append(Check("activity", passed, detail, swaps))
        if self.activity is not None and self.s.activity_enabled and hasattr(self.activity, "drawdown"):
            try:
                crash = self.activity.drawdown(product, entry, None)
            except Exception as e:
                crash = None
                checks.append(Check("crash", True, f"could not read the price path: {type(e).__name__}", None))
            if crash is not None:
                ceiling = D(self.s.max_recent_drawdown)
                dd = D(str(crash["drawdown"]))
                hours = float(crash["window_seconds"]) / 3600
                checks.append(Check(
                    "crash", dd < ceiling,
                    f"{dd * 100:.0f}% below its {hours:.0f}h high over {crash['swaps']} swaps, ceiling {ceiling * 100:.0f}%",
                    str(dd),
                ))
        floor = D(self.s.min_market_cap_usd)
        supply = entry.get("total_supply")
        if floor > 0 and not supply and entry.get("address"):
            # The universe entry predates supply tracking: ask the token now
            # rather than skip the check.
            try:
                supply = str(int(self.client.total_supply(entry["address"])))
            except Exception:
                supply = None
        if floor > 0 and supply:
            try:
                qd = self.registry.quote_decimals
                unit = 10 ** int(entry["decimals"])
                out = self._quote_call(product)(entry["address"], self.registry.quote_address, unit, int(entry.get("pool_fee") or self.s.pool_fee_tier))
                price = from_wei(int(out), qd)
                cap = price * D(int(supply)) / D(unit)
                checks.append(Check("market_cap", cap >= floor, f"about ${cap:,.0f}, floor ${floor:,.0f}", str(cap)))
            except Exception as e:
                checks.append(Check("market_cap", True, f"could not price the supply: {type(e).__name__}", None))
        return checks

    # The wallet a live run trades from. Set by the run; None in paper mode.
    wallet = None

    def _executable(self, product, entry):
        """Dry-run the real buy through the router from the trading wallet.

        A quote only simulates the price maths; it never moves tokens. A token
        whose transfer refuses the router (a honeypot, a blacklist, "trading
        not open") quotes perfectly and reverts on execution. Only a live run
        with its approvals in place can ask this; anyone else gets no check.
        """
        venue = getattr(self.market, "venue", None)
        route = entry.get("route")
        if self.wallet is None or venue is None or not route or not hasattr(venue, "swap_call"):
            return None
        if self._venue(product) != "v4":
            return None
        from .v4 import describe_revert

        qd = self.registry.quote_decimals
        amount = to_wei(self.s.order_limit_usd, qd)
        # The dry-run can only mean something with approvals in place; the buy
        # path grants them, and it runs after this check. Ask directly rather
        # than read a missing approval as the token's fault.
        try:
            erc20 = self.client.erc20(self.registry.quote_address)
            allowance = int(erc20.functions.allowance(self.wallet, venue.permit2_address).call())
            permitted, expiration = venue.permit2_allowance(self.wallet, self.registry.quote_address)
            if allowance < amount or permitted < amount or expiration <= int(time.time()) + 60:
                return Check("executable", True, "approvals not in place yet; buy not simulated", None)
        except Exception:
            pass
        try:
            call = venue.swap_call(route, self.registry.quote_address, amount, 0, int(time.time()) + 120)
            call.call({"from": self.wallet})
        except Exception as e:
            names = {c.__name__ for c in type(e).__mro__}
            if not names & {"ContractLogicError", "ContractCustomError", "ContractPanicError"}:
                return None  # the network, not the pool, failed to answer
            why = describe_revert(e)
            if any(k in why for k in ("Allowance", "TRANSFER_FROM_FAILED", "InsufficientAllowance", "AllowanceExpired")):
                # The wallet's approvals, not the token: the run cannot tell, so it does not judge.
                return Check("executable", True, "approvals not in place yet; buy not simulated", None)
            if getattr(e, "data", None) in (None, "", "0x") and hasattr(self.client, "revert_reason"):
                second = self.client.revert_reason(call, self.wallet)
                if second:
                    why = f"{why} ({second})"
            return Check("executable", False, f"a buy at the run's order size {why}", why)
        return Check("executable", True, "a buy at the run's order size simulates", True)

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
        if self._venue(product) == "v4":
            return self._pool_v4(product, entry, pool, eth_usd)
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
        checks += self._probes(product, entry, quote_token, fee, decimals, qd)
        return checks

    def _pool_v4(self, product, entry, pool, eth_usd=None):
        """v4 pools live inside one singleton and carry no oracle. Depth is read
        from the pool's liquidity when the venue exposes it, else inferred from
        the probes; age comes from the block the pool was initialised in."""
        checks = []
        quote_token = self.registry.quote_address
        fee = int(entry.get("pool_fee") or self.s.pool_fee_tier)
        decimals = entry["decimals"]
        qd = self.registry.quote_decimals
        age = self._pool_age(entry)
        checks.append(
            Check(
                "pool_age",
                age is not None and age >= self.s.min_pool_age_seconds,
                f"pool is {int(age)}s old, floor {int(self.s.min_pool_age_seconds)}s"
                if age is not None
                else "pool creation block unknown",
                age,
            )
        )
        state = self._v4_state(entry, eth_usd)
        if state is not None and state["liquidity"] == 0:
            # Pons initialises the pool when the token is created and adds the
            # liquidity when it graduates. Nothing to probe yet; look again later.
            checks.append(
                Check("pool_empty", False, "pool holds no liquidity yet; screened again later", "0")
            )
            return checks
        probes = self._probes(product, entry, quote_token, fee, decimals, qd)
        impact = next((c for c in probes if c.name == "price_impact"), None)
        floor = self.min_liquidity_usd()
        if state is not None and state.get("depth_usd") is not None:
            depth = state["depth_usd"]
            how = "from pool liquidity"
        elif impact is not None and impact.value is not None:
            # A route that moves the price by x% for an order of N dollars has
            # about N/x dollars of effective depth on the way in; both sides
            # together is roughly twice that. The pool's own fee is not impact.
            lp_fee = D(state["lpFee"]) / D(1000000) if state and state.get("lpFee") is not None else D(0)
            net = D(impact.value) - lp_fee
            if net > D("0.0001"):
                depth = D(self.s.order_limit_usd) / net * 2
            else:
                depth = D(self.s.order_limit_usd) * 2000  # no measurable impact at this size
            how = "inferred from price impact"
        else:
            depth = D(0)
            how = "unknown: the route did not quote"
        checks.append(
            Check(
                "liquidity",
                depth >= floor,
                f"route depth about ${depth:.0f} {how}, floor ${floor:.0f}",
                str(depth),
            )
        )
        return checks + probes

    def _v4_state(self, entry, eth_usd):
        """Liquidity, current fee and dollar depth of the token's own pool, from
        the StateView. None when the venue cannot say."""
        venue = getattr(self.market, "venue", None)
        route = entry.get("route") or []
        if venue is None or not route or not hasattr(venue, "liquidity"):
            return None
        key = route[-1]
        try:
            liquidity = int(venue.liquidity(key))
            slot0 = venue.slot0(key)
        except Exception:
            return None
        state = {"liquidity": liquidity, "lpFee": slot0.get("lpFee"), "depth_usd": None}
        if liquidity == 0 or not slot0.get("sqrtPriceX96"):
            return state
        token = checksum(entry["address"])
        other = key["currency1"] if checksum(key["currency0"]) == token else key["currency0"]
        sqrt_p = int(slot0["sqrtPriceX96"])
        # Virtual reserves of a full-range position; concentrated liquidity
        # makes this an over-estimate near the price and the floor is a floor.
        if checksum(other) == checksum(key["currency1"]):
            reserve = liquidity * sqrt_p // 2**96
        else:
            reserve = liquidity * 2**96 // sqrt_p
        try:
            usd = self._currency_usd(other, reserve, eth_usd)
        except Exception:
            usd = None
        if usd is not None:
            state["depth_usd"] = usd * 2
        return state

    def _currency_usd(self, currency, amount_wei, eth_usd):
        """Dollar value of `amount_wei` of a v4 currency: USDG, ETH/WETH, or a
        bridge asset priced through its USDG pool."""
        from . import v4 as v4mod

        currency = checksum(currency)
        qd = self.registry.quote_decimals
        if currency == self.registry.quote_address:
            return from_wei(amount_wei, qd)
        if currency in (v4mod.NATIVE, self.registry.weth):
            if eth_usd is None:
                return None
            return from_wei(amount_wei, 18) * D(eth_usd)
        bridge = (self.l.get("bridges") or {}).get(currency)
        if not bridge:
            return None
        venue = self.market.venue
        decimals = int(self.client.token_identity(currency)["decimals"])
        unit = 10**decimals
        out = venue.quote_path(bridge["route"], currency, unit)
        return from_wei(int(out), qd) * D(amount_wei) / D(unit)

    def _pool_age(self, entry):
        block = entry.get("discovered_block") or entry.get("initialized_block")
        if not block:
            return None
        try:
            latest = self.client.w3.eth.get_block("latest")
            created = int(self.client.w3.eth.get_block(int(block))["timestamp"])
            return float(int(latest["timestamp"]) - created)
        except Exception:
            pass
        # The public RPC turned a block lookup down: fall back to the block
        # distance at the chain's observed pace.
        try:
            head = int(self.client.w3.eth.block_number)
        except Exception:
            return None
        return float(max(0, head - int(block)) * self._seconds_per_block())

    SECONDS_PER_BLOCK_FALLBACK = 0.25

    def _seconds_per_block(self):
        cached = getattr(self, "_spb", None)
        if cached:
            return cached
        try:
            latest = self.client.w3.eth.get_block("latest")
            earlier = self.client.w3.eth.get_block(int(latest["number"]) - 5000)
            spb = (int(latest["timestamp"]) - int(earlier["timestamp"])) / 5000
            self._spb = spb if spb > 0 else self.SECONDS_PER_BLOCK_FALLBACK
        except Exception:
            self._spb = self.SECONDS_PER_BLOCK_FALLBACK
        return self._spb

    def _probes(self, product, entry, quote_token, fee, decimals, qd):
        checks = []
        order_probe = to_wei(self.s.order_limit_usd, qd)
        small_probe = max(1, order_probe // self.SMALL_PROBE_DIVISOR)
        quote_call = self._quote_call(product)
        small = self._round_trip(entry["address"], quote_token, small_probe, fee, decimals, quote_call)
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

        big = self._round_trip(entry["address"], quote_token, order_probe, fee, decimals, quote_call)
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

    def _round_trip(self, base, quote, probe_wei, fee, decimals, quote_call=None):
        """Fraction lost buying and immediately selling `probe_wei` of USDG."""
        quote_call = quote_call or self.quote_call
        try:
            base_out = quote_call(quote, base, probe_wei, fee)
            quote_back = quote_call(base, quote, base_out, fee)
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
