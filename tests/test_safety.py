"""The rug screen, and what happens after a rug gets through it.

Every chain answer here comes from an in-memory pool model. No RPC is made.
"""

import time

import pytest

from stonkflyrh.config import D, Settings, to_wei
from stonkflyrh.ledger import Ledger
from stonkflyrh.market import Quote
from stonkflyrh.risk import Guard, Veto
from stonkflyrh.safety import (
    DANGEROUS_SIGNATURES,
    RugScreen,
    RugWatch,
    Verdict,
    dangerous_selectors,
    selector,
)

WETH = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
POOL = "0x" + "33" * 20
ETH_USD = D("2500")
CAPITAL = D("100")
QD = 6


class Result:
    def __init__(self, value):
        self.value = value

    def call(self, *_a, **_k):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Functions:
    def __init__(self, chain):
        self.chain = chain

    def balanceOf(self, _address):
        return Result(self.chain.pool_weth)


class Contract:
    def __init__(self, chain):
        self.functions = Functions(chain)


class FakePool:
    """A constant-product-ish pool with a configurable transfer tax and depth."""

    def __init__(
        self,
        price=D("0.00025"),
        pool_fee=10000,
        tax=D("0"),
        depth=D("125000"),
        pool_weth=to_wei("50000", QD),
        upgradeable=False,
        selectors=(),
        owner=None,
        cardinality=64,
        sellable=True,
        has_code=True,
    ):
        self.price = price
        self.pool_fee = D(pool_fee) / D(1000000)
        self.tax = D(tax)
        self.depth = D(depth)
        self.pool_weth = pool_weth
        self.upgradeable = upgradeable
        self._selectors = set(selectors)
        self.owner = owner
        self.cardinality = cardinality
        self.sellable = sellable
        self._has_code = has_code

    # -- chain client surface -----------------------------------------------

    def has_code(self, _address):
        return self._has_code

    def is_upgradeable(self, _address):
        return self.upgradeable

    def selectors(self, _address):
        return set(self._selectors)

    def contract(self, _address, _abi):
        return Contract(self)

    def erc20(self, address):
        return self.contract(address, None)

    def try_call(self, _address, _abi, function, *_args):
        if function == "slot0":
            return (0, 0, 0, self.cardinality, self.cardinality, 0, True)
        if function == "owner":
            return self.owner
        return None

    # -- the quoter ---------------------------------------------------------

    def quote(self, token_in, _token_out, amount_in, _fee):
        """6-decimal USDG against an 18-decimal memecoin; depth is in USDG."""
        net = D(amount_in) * (1 - self.pool_fee) * (1 - self.tax)
        scale = D(10) ** 12
        if token_in.lower() == WETH.lower():
            impact = min(D("0.9"), D(amount_in) / (self.depth * D(10) ** QD))
            return int(net * (1 - impact) / self.price * scale)
        if not self.sellable:
            return 0
        out = net * self.price / scale
        impact = min(D("0.9"), out / (self.depth * D(10) ** QD))
        return int(out * (1 - impact))


class FakeRegistry:
    quote_address = WETH
    quote_decimals = QD

    def token(self, symbol):
        return {"symbol": symbol, "address": TOKEN, "decimals": 18}

    def pool_fee(self, _symbol, default):
        return default


def build(tmp_path, pool=None, **overrides):
    settings = Settings(**overrides)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper", CAPITAL)
    chain = pool or FakePool()
    screen = RugScreen(settings, chain, FakeRegistry(), ledger, chain.quote)
    return settings, ledger, screen, chain


def quote_for(price=D("0.00025"), **changes):
    q = Quote(
        "PONS", price * D("0.99"), price * D("1.01"), time.time(), 18,
        QD, 10000, D("10"), D("40000"),
    )
    import dataclasses

    return dataclasses.replace(q, **changes)


# -- selector table --------------------------------------------------------


def test_selector_matches_known_signatures():
    # The canonical ERC-20 transfer selector, as a check on the hashing itself.
    assert selector("transfer(address,uint256)") == "a9059cbb"


def test_every_dangerous_signature_hashes_to_a_distinct_selector():
    table = dangerous_selectors()
    flat = [sel for group in table.values() for sel in group]
    assert len(flat) == len(set(flat))
    assert set(table) == set(DANGEROUS_SIGNATURES)


# -- the screen ------------------------------------------------------------


def test_a_clean_token_is_approved(tmp_path):
    _, ledger, screen, _ = build(tmp_path)
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        assert verdict.approved, verdict.reason()
        assert {c.name for c in verdict.checks} == {
            "contract_code", "upgradeable", "owner_levers", "ownership",
            "liquidity", "pool_history", "sellable", "transfer_tax", "price_impact",
        }
    finally:
        ledger.close()


def test_a_honeypot_is_caught_by_the_sell_leg(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(sellable=False))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        assert not verdict.approved
        assert "could not be sold back" in verdict.reason()
    finally:
        ledger.close()


def test_a_transfer_tax_is_measured_and_rejected(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(tax=D("0.15")))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        tax = next(c for c in verdict.checks if c.name == "transfer_tax")
        assert not tax.passed
        # The small probe carries almost no impact, so what it loses beyond the
        # pool fee is the tax. 15% each way should read back close to 15%.
        assert D("0.12") < D(tax.value) < D("0.18")
    finally:
        ledger.close()


def test_a_small_tax_inside_the_ceiling_passes(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(tax=D("0.01")))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        assert verdict.approved, verdict.reason()
    finally:
        ledger.close()


def test_a_thin_pool_fails_price_impact(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(depth=D("150")))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        impact = next(c for c in verdict.checks if c.name == "price_impact")
        assert not impact.passed
    finally:
        ledger.close()


def test_shallow_liquidity_is_rejected(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(pool_weth=to_wei("900", QD)))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        liquidity = next(c for c in verdict.checks if c.name == "liquidity")
        assert not liquidity.passed
        assert "floor" in liquidity.detail
    finally:
        ledger.close()


def test_an_upgradeable_token_is_rejected(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(upgradeable=True))
    try:
        assert not screen.assess("PONS", POOL, ETH_USD).approved
    finally:
        ledger.close()


def test_the_proxy_check_can_be_turned_off(tmp_path):
    _, ledger, screen, _ = build(
        tmp_path, pool=FakePool(upgradeable=True), reject_upgradeable=False
    )
    try:
        assert screen.assess("PONS", POOL, ETH_USD).approved
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "signature", ["mint(address,uint256)", "blacklist(address,bool)", "setFee(uint256)"]
)
def test_an_owner_lever_in_bytecode_is_rejected(tmp_path, signature):
    pool = FakePool(selectors=[selector(signature)])
    _, ledger, screen, _ = build(tmp_path, pool=pool)
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        levers = next(c for c in verdict.checks if c.name == "owner_levers")
        assert not levers.passed
        assert signature in levers.detail
    finally:
        ledger.close()


def test_unrenounced_ownership_is_rejected(tmp_path):
    owner = "0x" + "99" * 20
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(owner=owner))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        ownership = next(c for c in verdict.checks if c.name == "ownership")
        assert not ownership.passed
    finally:
        ledger.close()


def test_a_renounced_owner_passes(tmp_path):
    zero = "0x" + "00" * 20
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(owner=zero))
    try:
        assert screen.assess("PONS", POOL, ETH_USD).approved
    finally:
        ledger.close()


def test_a_young_pool_without_oracle_history_is_rejected(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(cardinality=1))
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        history = next(c for c in verdict.checks if c.name == "pool_history")
        assert not history.passed
    finally:
        ledger.close()


def test_a_screen_that_throws_is_a_rejection_not_an_approval(tmp_path):
    class Broken(FakePool):
        def has_code(self, _address):
            raise RuntimeError("node unavailable")

    _, ledger, screen, _ = build(tmp_path, pool=Broken())
    try:
        verdict = screen.assess("PONS", POOL, ETH_USD)
        assert not verdict.approved
        assert "could not complete" in verdict.reason()
    finally:
        ledger.close()


def test_verdicts_are_cached_then_expire(tmp_path):
    calls = []

    class Counting(FakePool):
        def has_code(self, address):
            calls.append(address)
            return True

    settings, ledger, screen, _ = build(
        tmp_path, pool=Counting(), screen_ttl_seconds=100
    )
    try:
        now = time.time()
        screen.assess("PONS", POOL, ETH_USD, now)
        screen.assess("PONS", POOL, ETH_USD, now + 10)
        assert len(calls) == 1
        screen.assess("PONS", POOL, ETH_USD, now + 200)
        assert len(calls) == 2
        screen.assess("PONS", POOL, ETH_USD, now + 200, force=True)
        assert len(calls) == 3
    finally:
        ledger.close()


def test_a_verdict_round_trips_through_json(tmp_path):
    _, ledger, screen, _ = build(tmp_path)
    try:
        original = screen.assess("PONS", POOL, ETH_USD)
        restored = Verdict.from_json(original.json())
        assert restored.approved == original.approved
        assert [c.name for c in restored.checks] == [c.name for c in original.checks]
    finally:
        ledger.close()


# -- the screen as a veto --------------------------------------------------


def test_require_raises_for_a_rejected_token(tmp_path):
    _, ledger, screen, _ = build(tmp_path, pool=FakePool(sellable=False))
    try:
        with pytest.raises(Veto, match="rug screen rejected"):
            screen.require("PONS", POOL, ETH_USD)
    finally:
        ledger.close()


def test_require_raises_for_a_blocklisted_token(tmp_path):
    _, ledger, screen, _ = build(tmp_path)
    try:
        ledger.block("PONS", "rugged earlier", time.time())
        with pytest.raises(Veto, match="blocklisted"):
            screen.require("PONS", POOL, ETH_USD)
    finally:
        ledger.close()


def test_a_disabled_screen_still_honours_the_blocklist(tmp_path):
    _, ledger, screen, _ = build(tmp_path, screen_enabled=False)
    try:
        assert screen.require("PONS", POOL, ETH_USD) is None
        ledger.block("PONS", "rugged earlier", time.time())
        with pytest.raises(Veto, match="blocklisted"):
            screen.require("PONS", POOL, ETH_USD)
    finally:
        ledger.close()


def test_the_screen_blocks_buys_but_never_sells(tmp_path):
    settings, ledger, screen, _ = build(tmp_path, pool=FakePool(sellable=False))
    try:
        guard = Guard(settings, ledger, tmp_path / "STOP", screen)
        quotes = {"PONS": quote_for()}
        with pytest.raises(Veto, match="rug screen rejected"):
            guard.plan("PONS", "BUY", quotes, ETH_USD)
        # The way out of a bad token must always stay open.
        ledger.put("positions", {"PONS": "40000"})
        plan = guard.plan("PONS", "SELL", quotes, ETH_USD)
        assert plan["side"] == "SELL"
    finally:
        ledger.close()


# -- learning from a rug ---------------------------------------------------


def test_each_rug_tightens_the_thresholds(tmp_path):
    _, ledger, screen, _ = build(tmp_path)
    try:
        base_liquidity = screen.min_liquidity_usd()
        base_tax = screen.max_transfer_tax()
        ledger.record_rug(
            {"product": "SCAM", "at": time.time(), "reason": "bid collapsed", "drawdown": "0.9"}
        )
        assert screen.min_liquidity_usd() > base_liquidity
        assert screen.max_transfer_tax() < base_tax
        assert screen.thresholds()["rugs_recorded"] == 1
    finally:
        ledger.close()


def test_tightening_is_capped_so_the_screen_cannot_seize_up(tmp_path):
    _, ledger, screen, _ = build(tmp_path)
    try:
        for i in range(10):
            ledger.record_rug(
                {"product": f"S{i}", "at": time.time(), "reason": "x", "drawdown": "0.9"}
            )
        assert screen.tightening() == D(Settings().rug_tightening) ** 4
    finally:
        ledger.close()


def test_adaptation_off_means_static_thresholds(tmp_path):
    _, ledger, screen, _ = build(tmp_path, adapt_enabled=False)
    try:
        ledger.record_rug(
            {"product": "SCAM", "at": time.time(), "reason": "x", "drawdown": "0.9"}
        )
        assert screen.tightening() == D(1)
    finally:
        ledger.close()


# -- the rug watch ---------------------------------------------------------


def watch_for(tmp_path, **overrides):
    settings = Settings(**overrides)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper", CAPITAL)
    return settings, ledger, RugWatch(settings, ledger)


def test_a_collapsing_position_is_recorded_as_a_rug(tmp_path):
    _, ledger, watch = watch_for(tmp_path)
    try:
        ledger.put("positions", {"PONS": "40000"})
        watch.record_entry("PONS", D("0.00025"))
        collapsed = quote_for(price=D("0.000025"))
        record = watch.inspect("PONS", collapsed)
        assert record is not None
        assert D(record["drawdown"]) > D("0.5")
        assert ledger.is_blocked("PONS")
        assert len(ledger.rugs()) == 1
    finally:
        ledger.close()


def test_a_rug_is_recorded_once_however_long_the_exit_takes(tmp_path):
    _, ledger, watch = watch_for(tmp_path)
    try:
        ledger.put("positions", {"PONS": "40000"})
        watch.record_entry("PONS", D("0.00025"))
        collapsed = quote_for(price=D("0.000025"))
        assert watch.inspect("PONS", collapsed) is not None
        # Still holding some of it on the next observation: same rug, no new row.
        assert watch.inspect("PONS", collapsed) is None
        assert len(ledger.rugs()) == 1
    finally:
        ledger.close()


def test_an_ordinary_drawdown_is_not_a_rug(tmp_path):
    _, ledger, watch = watch_for(tmp_path)
    try:
        ledger.put("positions", {"PONS": "40000"})
        watch.record_entry("PONS", D("0.00025"))
        assert watch.inspect("PONS", quote_for(price=D("0.0002"))) is None
        assert not ledger.is_blocked("PONS")
    finally:
        ledger.close()


def test_no_position_means_nothing_to_watch(tmp_path):
    _, ledger, watch = watch_for(tmp_path)
    try:
        watch.record_entry("PONS", D("0.00025"))
        assert watch.inspect("PONS", quote_for(price=D("0.0000025"))) is None
    finally:
        ledger.close()


def test_the_entry_reference_is_the_worst_price_paid(tmp_path):
    _, ledger, watch = watch_for(tmp_path)
    try:
        watch.record_entry("PONS", D("0.00025"))
        watch.record_entry("PONS", D("0.0001"))
        assert watch.entry_price("PONS") == D("0.00025")
    finally:
        ledger.close()


def test_a_rug_earns_a_longer_aversive_pulse(tmp_path):
    settings, ledger, watch = watch_for(tmp_path)
    try:
        assert watch.pulse_ms() > settings.pulse_ms
        assert watch.pulse_ms() <= settings.neural_ms
    finally:
        ledger.close()


def test_closing_a_position_clears_its_rug_reference(tmp_path):
    from stonkflyrh.broker import PaperBroker

    settings, ledger, watch = watch_for(tmp_path)
    try:
        guard = Guard(settings, ledger, tmp_path / "STOP")
        quotes = {"PONS": quote_for()}
        plan = ledger.reserve(guard.plan("PONS", "BUY", quotes, ETH_USD), time.time())
        PaperBroker(settings, ledger).execute(plan, guard.before_submit)
        watch.record_entry("PONS", quotes["PONS"].ask)
        ledger.put("last_attempt", 0)
        plan = ledger.reserve(guard.plan("PONS", "SELL", quotes, ETH_USD), time.time())
        PaperBroker(settings, ledger).execute(plan, guard.before_submit)
        assert "PONS" not in ledger.positions           # sold to nothing: off the books
        assert watch.entry_price("PONS") is None
    finally:
        ledger.close()


def test_the_screen_asks_whether_anyone_is_here(tmp_path):
    """The activity check: a token with one swap an hour is withheld even when
    its pool clears the depth floor. Market cap is never asked, however small."""
    from stonkflyrh.safety import RugScreen

    settings = Settings(min_recent_swaps=5)
    ledger = Ledger(tmp_path / "l.sqlite", settings, "paper", CAPITAL)
    chain = FakePool()
    try:
        screen = RugScreen(settings, chain, FakeRegistry(), ledger, chain.quote)

        class Quiet:
            def enough(self, product, entry, now):
                return False, "1 swap in the last 60 min, floor 5", 1

        screen.activity = Quiet()
        # 1e9 tokens, priced by the fake pool at 0.00025 USDG each: a $250K cap.
        entry = {"address": TOKEN, "decimals": 18, "pool_fee": 10000, "total_supply": str(10**9 * 10**18)}
        checks = {c.name: c for c in screen._market("PONS", entry)}
        assert not checks["activity"].passed
        assert "market_cap" not in checks
        # A supply a thousand times smaller is a $250 cap; still not a question the screen asks.
        small = {**entry, "total_supply": str(10**6 * 10**18)}
        assert "market_cap" not in {c.name for c in screen._market("PONS", small)}
    finally:
        ledger.close()


def test_a_cached_verdict_from_an_older_screen_is_not_trusted(tmp_path):
    from stonkflyrh.safety import RugScreen

    _, ledger, screen, chain = build(tmp_path)
    try:
        first = screen.assess("PONS", POOL, ETH_USD, now=1000.0)
        assert first.approved
        # A verdict written by an older screen version: rerun, not reused.
        stale = {**first.json(), "screen_version": RugScreen.SCREEN_VERSION - 1}
        ledger.screen_put("PONS", stale, 1000.0)
        chain.sellable = False
        again = screen.assess("PONS", POOL, ETH_USD, now=1001.0)
        assert not again.approved
    finally:
        ledger.close()
