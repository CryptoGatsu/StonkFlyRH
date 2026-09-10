"""No test sends a transaction. Chain calls here are in-memory doubles."""

import dataclasses
import time

import pytest
from pydantic import ValidationError

from stonkflyrh.actions import Proposal, StonkflyRHActions
from stonkflyrh.broker import PaperBroker
from stonkflyrh.config import D, QUOTE_DECIMALS, Settings, from_wei, to_wei
from stonkflyrh.ledger import Ledger
from stonkflyrh.market import Quote
from stonkflyrh.reinforcement import reinforcement
from stonkflyrh.risk import Guard, Veto, realised_volatility

# Memecoin price in USDG; the ledger is dollars, so capital is the stake itself.
PRICE = D("0.00025")
ETH_USD = D("2500")
CAPITAL = D("100")


def quote(**changes):
    q = Quote(
        "PONS",
        PRICE * D("0.99"),
        PRICE * D("1.01"),
        time.time(),
        18,
        QUOTE_DECIMALS,
        10000,
        D("10"),
        D("39604"),
    )
    return dataclasses.replace(q, **changes)


@pytest.fixture
def env(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper", CAPITAL)
    guard = Guard(s, ledger, tmp_path / "STOP")
    yield s, ledger, guard
    ledger.close()


def buy(env, quotes=None):
    s, ledger, guard = env
    quotes = quotes or {"PONS": quote()}
    plan = ledger.reserve(guard.plan("PONS", "BUY", quotes, ETH_USD), time.time())
    return PaperBroker(s, ledger).execute(plan, guard.before_submit)


# -- configuration ---------------------------------------------------------


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True])
def test_nonfinite_money(value):
    with pytest.raises(ValueError):
        D(value)


@pytest.mark.parametrize(
    "changes",
    [
        dict(capital_usd="1001"),
        dict(order_limit_usd="101"),
        dict(order_limit_usd="200", capital_usd="150"),
        dict(min_order_usd="20"),
        dict(loss_stop_usd="200"),
        dict(products=("USDG",)),
        dict(products=("PONS", "PONS")),
        dict(products=("pons",)),
        dict(products=("A_B", "A-B")),
        dict(max_products=3, products=("A", "B", "C", "D")),
        dict(discovery_interval_seconds=30),
        dict(products=(), discovery_enabled=False),
        dict(network="ethereum"),
        dict(protocol_fee_bps=301),
        dict(protocol_fee_bps=1.0),
        dict(pool_fee_tier=1234),
        dict(slippage="0.2"),
        dict(max_gas_share="0.9"),
        dict(gas_limit=99),
        dict(neural_bin_ms=0.01),
        dict(reward_deadband_usd="0"),
        dict(interval_seconds=float("nan")),
        dict(daily_orders=1.5),
        dict(max_transfer_tax="0.9"),
        dict(max_price_impact="0"),
        dict(rug_drawdown="1"),
        dict(min_pool_observations=-1),
        dict(calm_volatility="0.5", max_volatility="0.2"),
        dict(min_size_scale="0"),
        dict(rug_tightening="0.5"),
        dict(rug_pulse_multiplier="9"),
        dict(rug_exit_spread="0.01"),
        dict(volatility_window=2),
    ],
)
def test_configuration_bounds(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


def test_defaults_match_the_operators_stated_size():
    s = Settings()
    assert s.capital_usd == "100"
    assert s.order_limit_usd == "10"
    assert (s.quote_symbol, s.quote_decimals) == ("USDG", 6)
    assert s.network == "robinhood-mainnet"
    assert s.screen_enabled and s.adapt_enabled


def test_seeds_are_optional_when_discovery_is_on():
    assert Settings(products=()).products == ()
    with pytest.raises(ValueError, match="at least one seed"):
        Settings(products=(), discovery_enabled=False)


def test_only_robinhood_chain_is_configurable():
    assert Settings(network="robinhood-testnet").network == "robinhood-testnet"
    with pytest.raises(ValueError):
        Settings(network="base-mainnet")


# -- dollar limits ---------------------------------------------------------


def test_limits_are_the_dollar_settings_because_usdg_is_dollars(env):
    _, _, guard = env
    limits = guard.limits(ETH_USD)
    assert limits["order_limit"] == D("10")
    assert limits["min_order"] == D("1")
    # ETH moving changes what gas costs, never what a $10 order is.
    assert guard.limits(D("5000"))["order_limit"] == limits["order_limit"]


def test_a_ten_dollar_order_is_ten_usdg_whatever_eth_does(env):
    s, _, guard = env
    for price in [D("1500"), D("2500"), D("4000")]:
        plan = guard.plan("PONS", "BUY", {"PONS": quote()}, price)
        assert D(plan["notional_usd"]) == D("10")
        assert int(plan["amount_in_wei"]) == to_wei("10", QUOTE_DECIMALS)
        guard.l.put("last_attempt", 0)


def test_gas_is_valued_in_dollars_inside_equity(env):
    _, ledger, _ = env
    ledger.put("gas_spent", "0.001")  # ETH
    quotes = {"PONS": quote()}
    assert ledger.equity(quotes, D("2500")) == CAPITAL - D("2.5")
    assert ledger.equity(quotes, D("5000")) == CAPITAL - D("5")


# -- the guard -------------------------------------------------------------


def test_stop_file_vetoes(env, tmp_path):
    _, _, guard = env
    (tmp_path / "STOP").write_text("")
    with pytest.raises(Veto, match="STOP"):
        guard.check({"PONS": quote()}, time.time(), ETH_USD)


def test_halt_vetoes(env):
    _, ledger, guard = env
    ledger.halt("manual")
    with pytest.raises(Veto, match="manual"):
        guard.check({"PONS": quote()}, time.time(), ETH_USD)


def test_stale_and_future_quotes_veto(env):
    _, _, guard = env
    now = time.time()
    with pytest.raises(Veto, match="Stale"):
        guard.check({"PONS": quote(timestamp=now - 3600)}, now, ETH_USD)
    with pytest.raises(Veto, match="Stale"):
        guard.check({"PONS": quote(timestamp=now + 60)}, now, ETH_USD)


def test_wide_round_trip_vetoes_a_buy(env):
    _, _, guard = env
    with pytest.raises(Veto, match="Round-trip"):
        guard.plan("PONS", "BUY", {"PONS": quote(bid=PRICE * D("0.5"))}, ETH_USD)


def test_one_drained_pool_does_not_freeze_the_other_tokens(tmp_path):
    s = Settings(products=("PONS", "DEAD"))
    ledger = Ledger(tmp_path / "two.sqlite", s, "paper", CAPITAL)
    try:
        guard = Guard(s, ledger, tmp_path / "STOP")
        quotes = {"PONS": quote(), "DEAD": quote(product="DEAD", bid=PRICE * D("0.3"))}
        guard.check(quotes, time.time(), ETH_USD)  # the tick itself is fine
        assert guard.plan("PONS", "BUY", quotes, ETH_USD)["product"] == "PONS"
        with pytest.raises(Veto, match="Round-trip"):
            guard.plan("DEAD", "BUY", quotes, ETH_USD)
    finally:
        ledger.close()


def test_the_exit_from_a_rug_may_pay_a_wide_spread(env):
    s, ledger, guard = env
    ledger.put("positions", {"PONS": "40000"})
    wide = {"PONS": quote(bid=PRICE * D("0.7"))}
    with pytest.raises(Veto, match="Round-trip"):
        guard.plan("PONS", "SELL", wide, ETH_USD)
    ledger.block("PONS", "bid collapsed", time.time())
    plan = guard.plan("PONS", "SELL", wide, ETH_USD)
    assert plan["side"] == "SELL"
    assert guard.move_tolerance("PONS", "SELL") == D(s.rug_exit_spread)
    assert guard.move_tolerance("PONS", "BUY") == D(s.slippage)


def test_volatility_never_stops_an_exit(env):
    _, ledger, guard = env
    ledger.put("positions", {"PONS": "40000"})
    wild = [1 + 0.9 * (-1) ** i for i in range(40)]
    with pytest.raises(Veto, match="volatility"):
        guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD, history=wild)
    plan = guard.plan("PONS", "SELL", {"PONS": quote()}, ETH_USD, history=wild)
    assert plan["size_scale"] == "1"


def test_incomplete_snapshot_vetoes(env):
    _, _, guard = env
    with pytest.raises(Veto, match="Incomplete"):
        guard.check({}, time.time(), ETH_USD)


def test_cooldown_and_daily_limit(env):
    s, ledger, guard = env
    buy(env)
    with pytest.raises(Veto, match="cooldown"):
        guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD)
    ledger.put("last_attempt", 0)
    for i in range(s.daily_orders):
        ledger.db.execute(
            "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
            (f"filler-{i}", "SETTLED", time.time(), "{}"),
        )
    with pytest.raises(Veto, match="Daily order limit"):
        guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD)


def test_unknown_product_or_side_vetoes(env):
    _, _, guard = env
    with pytest.raises(Veto, match="Invalid neural proposal"):
        guard.plan("SHIB", "BUY", {"PONS": quote()}, ETH_USD)


def test_gas_share_veto(env):
    _, _, guard = env
    # 500k gas at 10 gwei is 0.005 ETH = $12.50 against a $10 order.
    with pytest.raises(Veto, match="Gas cost"):
        guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD, gas_price_wei=10**10)
    # At 0.1 gwei it is 12.5 cents: fine.
    guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD, gas_price_wei=10**8)


def test_sell_without_position_vetoes(env):
    _, _, guard = env
    with pytest.raises(Veto, match="No position"):
        guard.plan("PONS", "SELL", {"PONS": quote()}, ETH_USD)


def test_loss_stop_halts(env):
    _, ledger, guard = env
    ledger.put("cash", str(CAPITAL - D("26")))
    with pytest.raises(Veto, match="Loss stop"):
        guard.check({"PONS": quote()}, time.time(), ETH_USD)
    assert "Loss stop" in ledger.get("halted")


def test_pending_order_blocks_new_plans(env):
    _, ledger, guard = env
    ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    with pytest.raises(Veto, match="unresolved"):
        guard.check({"PONS": quote()}, time.time(), ETH_USD)


def test_blocklisted_token_is_refused_without_a_screen(env):
    _, ledger, guard = env
    ledger.block("PONS", "rugged earlier", time.time())
    with pytest.raises(Veto, match="blocklisted"):
        guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD)


# -- adaptation ------------------------------------------------------------


def test_volatility_is_none_without_enough_history():
    assert realised_volatility([1.0, 1.1], 30) is None


def test_volatility_rises_with_choppier_history():
    calm = [1 + 0.001 * i for i in range(40)]
    choppy = [1 + 0.1 * (-1) ** i for i in range(40)]
    assert realised_volatility(choppy, 30) > realised_volatility(calm, 30)


def test_calm_markets_trade_the_full_size(env):
    _, _, guard = env
    scale, _ = guard.size_scale([1 + 0.0001 * i for i in range(40)])
    assert scale == D(1)


def test_choppy_markets_shrink_the_order(env):
    s, _, guard = env
    history = [1 + 0.06 * (-1) ** i for i in range(40)]
    scale, vol = guard.size_scale(history)
    assert D(s.min_size_scale) <= scale < D(1)
    assert vol is not None


def test_extreme_volatility_stops_trading_entirely(env):
    _, _, guard = env
    history = [1 + 0.9 * (-1) ** i for i in range(40)]
    with pytest.raises(Veto, match="volatility"):
        guard.size_scale(history)


def test_a_shrunken_order_still_carries_the_right_dollar_figure(env):
    _, _, guard = env
    history = [1 + 0.06 * (-1) ** i for i in range(40)]
    plan = guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD, history=history)
    assert D(plan["size_scale"]) < D(1)
    assert D(plan["notional_usd"]) < D("10")


def test_adaptation_off_keeps_a_constant_size(env, tmp_path):
    s = Settings(adapt_enabled=False)
    ledger = Ledger(tmp_path / "flat.sqlite", s, "paper", CAPITAL)
    try:
        guard = Guard(s, ledger, tmp_path / "STOP")
        history = [1 + 0.06 * (-1) ** i for i in range(40)]
        assert guard.size_scale(history) == (D(1), None)
    finally:
        ledger.close()


def test_a_losing_streak_lengthens_the_cooldown(env):
    s, ledger, guard = env
    assert guard.cooldown_seconds() == D(s.interval_seconds)
    ledger.put("loss_streak", 3)
    assert guard.cooldown_seconds() > D(s.interval_seconds)


# -- planning --------------------------------------------------------------


def test_buy_plan_reserves_the_fee_before_swapping(tmp_path):
    s = Settings(protocol_fee_bps=100)
    ledger = Ledger(tmp_path / "fee.sqlite", s, "paper", CAPITAL)
    try:
        guard = Guard(s, ledger, tmp_path / "STOP")
        plan = guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD)
        notional = int(plan["notional_wei"])
        fee = int(plan["planned_fee_wei"])
        assert fee == notional * 100 // 10000
        assert int(plan["amount_in_wei"]) == notional - fee
        assert plan["fee_basis"] == "input"
    finally:
        ledger.close()


def test_buy_plan_min_out_honours_slippage(env):
    s, _, guard = env
    q = quote()
    plan = guard.plan("PONS", "BUY", {"PONS": q}, ETH_USD)
    expected = from_wei(int(plan["amount_in_wei"]), QUOTE_DECIMALS) / q.ask
    assert int(plan["min_out_wei"]) == to_wei(expected * (1 - D(s.slippage)), 18)
    assert plan["quote_decimals"] == QUOTE_DECIMALS
    assert int(plan["min_out_wei"]) < to_wei(expected, 18)


def test_plan_below_minimum_notional_vetoes(tmp_path):
    # A stop equal to the stake keeps the loss stop from firing first, so the
    # minimum-notional rule is the one under test.
    s = Settings(loss_stop_usd="100")
    ledger = Ledger(tmp_path / "small.sqlite", s, "paper", CAPITAL)
    try:
        guard = Guard(s, ledger, tmp_path / "STOP")
        ledger.put("cash", "0.20")  # 20 cents left
        with pytest.raises(Veto, match="below the configured minimum"):
            guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD)
    finally:
        ledger.close()


# -- paper execution and accounting ---------------------------------------


def test_paper_buy_books_cash_and_position(env):
    _, ledger, _ = env
    start = ledger.cash
    result = buy(env)
    assert result["status"] == "FILLED"
    spent = int(result["quote_wei"])
    assert ledger.cash == start - from_wei(spent + int(result["fee_wei"]), QUOTE_DECIMALS)
    assert ledger.positions["PONS"] > 0


def test_round_trip_buy_then_sell(env):
    s, ledger, guard = env
    buy(env)
    held = ledger.positions["PONS"]
    assert held > 0
    ledger.put("last_attempt", 0)
    plan = ledger.reserve(guard.plan("PONS", "SELL", {"PONS": quote()}, ETH_USD), time.time())
    result = PaperBroker(s, ledger).execute(plan, guard.before_submit)
    assert result["status"] == "FILLED"
    assert ledger.positions["PONS"] < held


def test_gas_is_charged_to_equity_but_not_to_cash(env):
    _, ledger, _ = env
    before_gas = ledger.gas_spent
    result = buy(env)
    assert int(result["gas_wei"]) > 0
    assert ledger.gas_spent > before_gas
    assert ledger.equity({"PONS": quote()}, ETH_USD) < CAPITAL


def test_settlement_is_idempotent_and_immutable(env):
    s, ledger, guard = env
    plan = ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    cid = plan["client_order_id"]
    first = PaperBroker(s, ledger).execute(plan, guard.before_submit)
    cash = ledger.cash
    ledger.settle(
        cid, int(first["base_wei"]), int(first["quote_wei"]),
        int(first["fee_wei"]), int(first["gas_wei"]),
    )
    assert ledger.cash == cash
    with pytest.raises(RuntimeError, match="changed after finalization"):
        ledger.settle(cid, int(first["base_wei"]) + 1, int(first["quote_wei"]), 0, 0)


def test_settlement_refuses_a_fill_beyond_the_reserved_input(env):
    _, ledger, guard = env
    plan = ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    with pytest.raises(RuntimeError, match="spent more than the reserved input"):
        ledger.settle(
            plan["client_order_id"], int(plan["min_out_wei"]),
            int(plan["amount_in_wei"]) + 1, int(plan["planned_fee_wei"]),
        )


def test_settlement_refuses_a_fill_below_the_slippage_bound(env):
    _, ledger, guard = env
    plan = ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    with pytest.raises(RuntimeError, match="below the slippage bound"):
        ledger.settle(
            plan["client_order_id"], int(plan["min_out_wei"]) - 1,
            int(plan["amount_in_wei"]), int(plan["planned_fee_wei"]),
        )


def test_rejected_intent_cannot_settle(env):
    _, ledger, guard = env
    plan = ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    ledger.mark(plan["client_order_id"], "REJECTED")
    with pytest.raises(RuntimeError, match="rejected intent"):
        ledger.settle(plan["client_order_id"], 1, 1, 0)


def test_before_submit_rejection_marks_the_intent(env):
    s, ledger, guard = env
    plan = ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    guard.stop_file.write_text("")
    with pytest.raises(Veto):
        PaperBroker(s, ledger).execute(plan, guard.before_submit)
    assert ledger.order(plan["client_order_id"])["status"] == "REJECTED"


def test_reconcile_settles_an_interrupted_paper_fill(env):
    s, ledger, guard = env
    ledger.reserve(guard.plan("PONS", "BUY", {"PONS": quote()}, ETH_USD), time.time())
    assert ledger.pending()
    PaperBroker(s, ledger).reconcile()
    assert not ledger.pending()


def test_a_changed_frozen_setting_refuses_to_reopen_a_ledger(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", CAPITAL)
    ledger.close()
    with pytest.raises(RuntimeError, match="donor_share '0.5' -> '0.4'"):
        Ledger(tmp_path / "l.sqlite", Settings(donor_share="0.4"), "paper")


def test_a_tuned_setting_is_recorded_not_refused(tmp_path):
    """Operators tune order size and screen thresholds between restarts; a
    live run must carry on and keep a record of what changed."""
    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", CAPITAL)
    ledger.close()
    tuned = Settings(order_limit_usd="5", min_liquidity_usd="2500")
    reopened = Ledger(tmp_path / "l.sqlite", tuned, "paper")
    try:
        assert reopened.get("settings") == tuned.signature()
        assert reopened.get("settings_full")["order_limit_usd"] == "5"
        changed = reopened.events("migration")[0]["settings_changed"]
        assert changed["order_limit_usd"] == ["10", "5"]
        assert changed["min_liquidity_usd"] == ["4000", "2500"]
    finally:
        reopened.close()


def test_an_added_setting_migrates_the_ledger(tmp_path):
    """An upgrade that adds a field must not force a fresh run."""
    import dataclasses

    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", CAPITAL)
    stored = dict(ledger.get("settings_full"))
    stored.pop("coin_address")                      # pretend the run predates this field
    ledger.put("settings_full", stored)
    ledger.put("settings", "old-signature")
    ledger.close()
    reopened = Ledger(tmp_path / "l.sqlite", Settings(), "paper")
    try:
        assert reopened.get("settings") == Settings().signature()
        expected = dataclasses.asdict(Settings())
        expected["products"] = list(expected["products"])   # JSON turns tuples into lists
        assert reopened.get("settings_full") == expected
        assert reopened.events("migration")[0]["settings_added"] == ["coin_address"]
    finally:
        reopened.close()


def test_mode_mismatch_refuses_to_reopen_a_ledger(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", s, "paper", CAPITAL)
    ledger.close()
    with pytest.raises(RuntimeError, match="mismatch"):
        Ledger(tmp_path / "l.sqlite", s, "live")


def test_a_new_ledger_needs_its_starting_balance(tmp_path):
    with pytest.raises(ValueError, match="starting USDG balance"):
        Ledger(tmp_path / "l.sqlite", Settings(), "paper")


# -- the action boundary ---------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"product": "PONS", "side": "HOLD"},
        {"product": "PONS"},
        {"product": "PONS", "side": "BUY", "amount": "1"},
    ],
)
def test_proposal_schema_rejects_anything_but_a_side(args):
    with pytest.raises(ValidationError):
        Proposal.model_validate(args)


def test_provider_exposes_exactly_one_action(env):
    s, ledger, guard = env
    actions = StonkflyRHActions(guard, PaperBroker(s, ledger), "robinhood-mainnet").get_actions()
    assert len(actions) == 1
    assert actions[0].name == "stonkflyrh_swap"


def test_provider_only_supports_its_own_network(env):
    s, ledger, guard = env
    provider = StonkflyRHActions(guard, PaperBroker(s, ledger), "robinhood-mainnet")

    class Net:
        protocol_family = "evm"
        network_id = "robinhood-mainnet"

    assert provider.supports_network(Net())
    Net.network_id = "base-mainnet"
    assert not provider.supports_network(Net())


def test_provider_refuses_to_act_without_a_price_reference(env):
    s, ledger, guard = env
    provider = StonkflyRHActions(guard, PaperBroker(s, ledger), "robinhood-mainnet")
    provider.quotes = {"PONS": quote()}
    with pytest.raises(RuntimeError, match="ETH/USD reference"):
        provider.get_actions()[0].invoke({"product": "PONS", "side": "BUY"})


def test_provider_invoke_runs_the_guard(env):
    s, ledger, guard = env
    provider = StonkflyRHActions(guard, PaperBroker(s, ledger), "robinhood-mainnet")
    provider.quotes = {"PONS": quote()}
    provider.eth_usd = ETH_USD
    result = provider.get_actions()[0].invoke({"product": "PONS", "side": "BUY"})
    assert result["status"] == "FILLED"


# -- reinforcement ---------------------------------------------------------


@pytest.mark.parametrize(
    "equity,anchor,expected",
    [("100.10", "100", "reward"), ("99.90", "100", "aversive"), ("100.02", "100", "none")],
)
def test_reinforcement_signs(equity, anchor, expected):
    kind, _ = reinforcement(equity, anchor, "0.05")
    assert kind == expected
