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
from stonkflyrh.risk import Guard, Veto

PRICE = D("0.0000001")


def quote(**changes):
    q = Quote(
        "DOGE",
        PRICE * D("0.99"),
        PRICE * D("1.01"),
        time.time(),
        18,
        QUOTE_DECIMALS,
        10000,
        D("0.005"),
        D("49504"),
    )
    return dataclasses.replace(q, **changes)


@pytest.fixture
def env(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper")
    guard = Guard(s, ledger, tmp_path / "STOP")
    yield s, ledger, guard
    ledger.close()


def buy(env, quotes=None, now=None):
    s, ledger, guard = env
    quotes = quotes or {"DOGE": quote()}
    plan = guard.plan("DOGE", "BUY", quotes, now=now)
    plan = ledger.reserve(plan, now or time.time())
    return PaperBroker(s, ledger).execute(plan, guard.before_submit)


# -- configuration ---------------------------------------------------------


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True])
def test_nonfinite_money(value):
    with pytest.raises(ValueError):
        D(value)


@pytest.mark.parametrize(
    "changes",
    [
        dict(capital="1.01"),
        dict(order_limit="0.2"),
        dict(products=("WETH",)),
        dict(products=("DOGE", "DOGE")),
        dict(products=("doge",)),
        dict(products=()),
        dict(network="ethereum"),
        dict(protocol_fee_bps=301),
        dict(protocol_fee_bps=1.0),
        dict(pool_fee_tier=1234),
        dict(slippage="0.06"),
        dict(min_order_quote="0.9"),
        dict(max_gas_share="0.9"),
        dict(gas_limit=99),
        dict(neural_bin_ms=0.01),
        dict(reward_deadband="0"),
        dict(interval_seconds=float("nan")),
        dict(daily_orders=1.5),
    ],
)
def test_configuration_bounds(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


def test_defaults_are_denominated_in_weth():
    s = Settings()
    assert D(s.order_limit) <= D(s.capital)
    assert s.protocol_fee_bps == 100
    assert s.network == "robinhood-mainnet"


# -- the guard -------------------------------------------------------------


def test_stop_file_vetoes(env, tmp_path):
    _, _, guard = env
    (tmp_path / "STOP").write_text("")
    with pytest.raises(Veto, match="STOP"):
        guard.check({"DOGE": quote()}, time.time())


def test_halt_vetoes(env):
    _, ledger, guard = env
    ledger.halt("manual")
    with pytest.raises(Veto, match="manual"):
        guard.check({"DOGE": quote()}, time.time())


def test_stale_and_future_quotes_veto(env):
    _, _, guard = env
    now = time.time()
    with pytest.raises(Veto, match="Stale"):
        guard.check({"DOGE": quote(timestamp=now - 3600)}, now)
    with pytest.raises(Veto, match="Stale"):
        guard.check({"DOGE": quote(timestamp=now + 60)}, now)


def test_wide_round_trip_vetoes(env):
    _, _, guard = env
    wide = quote(bid=PRICE * D("0.5"))
    with pytest.raises(Veto, match="Round-trip"):
        guard.check({"DOGE": wide}, time.time())


def test_incomplete_snapshot_vetoes(env):
    _, _, guard = env
    with pytest.raises(Veto, match="Incomplete"):
        guard.check({}, time.time())


def test_cooldown_and_daily_limit(env):
    s, ledger, guard = env
    now = time.time()
    buy(env, now=now)
    with pytest.raises(Veto, match="cooldown"):
        guard.plan("DOGE", "BUY", {"DOGE": quote()}, now=now + 1)
    ledger.put("last_attempt", 0)
    for i in range(s.daily_orders):
        ledger.db.execute(
            "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
            (f"filler-{i}", "SETTLED", time.time(), "{}"),
        )
    with pytest.raises(Veto, match="Daily order limit"):
        guard.plan("DOGE", "BUY", {"DOGE": quote()})


def test_unknown_product_or_side_vetoes(env):
    _, _, guard = env
    with pytest.raises(Veto, match="Invalid neural proposal"):
        guard.plan("SHIB", "BUY", {"DOGE": quote()})


def test_gas_share_veto(env):
    _, _, guard = env
    # A gas price that would eat most of a 0.005 WETH order.
    with pytest.raises(Veto, match="Gas cost"):
        guard.plan("DOGE", "BUY", {"DOGE": quote()}, gas_price_wei=10**10)


def test_sell_without_position_vetoes(env):
    _, _, guard = env
    with pytest.raises(Veto, match="No position"):
        guard.plan("DOGE", "SELL", {"DOGE": quote()})


def test_loss_stop_halts(env):
    _, ledger, guard = env
    ledger.put("cash", "0.03")
    with pytest.raises(Veto, match="Loss stop"):
        guard.check({"DOGE": quote()}, time.time())
    assert "Loss stop" in ledger.get("halted")


def test_pending_order_blocks_new_plans(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    ledger.reserve(plan, time.time())
    with pytest.raises(Veto, match="unresolved"):
        guard.check({"DOGE": quote()}, time.time())


# -- planning --------------------------------------------------------------


def test_buy_plan_reserves_the_fee_before_swapping(env):
    s, _, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    notional = int(plan["notional_wei"])
    fee = int(plan["planned_fee_wei"])
    assert notional == to_wei(s.order_limit, QUOTE_DECIMALS)
    assert fee == notional * s.protocol_fee_bps // 10000
    assert int(plan["amount_in_wei"]) == notional - fee
    assert plan["fee_basis"] == "input"


def test_buy_plan_min_out_honours_slippage(env):
    s, _, guard = env
    q = quote()
    plan = guard.plan("DOGE", "BUY", {"DOGE": q})
    expected = from_wei(int(plan["amount_in_wei"]), QUOTE_DECIMALS) / q.ask
    assert int(plan["min_out_wei"]) == to_wei(expected * (1 - D(s.slippage)), 18)
    assert int(plan["min_out_wei"]) < to_wei(expected, 18)


def test_plan_below_minimum_notional_vetoes(tmp_path):
    s = Settings(capital="0.0006", order_limit="0.0006", loss_stop="0.0006",
                 min_order_quote="0.0005", gas_reserve="0.0002")
    ledger = Ledger(tmp_path / "l.sqlite", s, "paper")
    try:
        guard = Guard(s, ledger, tmp_path / "STOP")
        ledger.put("cash", "0.0001")
        with pytest.raises(Veto, match="below the configured minimum"):
            guard.plan("DOGE", "BUY", {"DOGE": quote()})
    finally:
        ledger.close()


# -- paper execution and accounting ---------------------------------------


def test_paper_buy_books_cash_position_and_fee(env):
    s, ledger, _ = env
    start = ledger.cash
    result = buy(env)
    assert result["status"] == "FILLED"
    fee = int(result["fee_wei"])
    spent = int(result["quote_wei"])
    assert ledger.cash == start - from_wei(spent + fee, QUOTE_DECIMALS)
    assert ledger.positions["DOGE"] > 0
    booked = ledger.fees.accrued()
    assert booked["gross_wei"] == fee
    assert booked["dev_wei"] == fee * 2 // 10
    assert booked["dev_wei"] + booked["treasury_wei"] == fee


def test_every_fill_books_exactly_twenty_percent_to_development(env):
    _, ledger, guard = env
    for _ in range(3):
        ledger.put("last_attempt", 0)
        try:
            buy(env)
        except Veto:
            break
    accrued = ledger.fees.accrued()
    assert accrued["fills_charged"] >= 1
    assert accrued["dev_wei"] * 10000 == accrued["gross_wei"] * 2000


def test_round_trip_buy_then_sell(env):
    s, ledger, guard = env
    buy(env)
    held = ledger.positions["DOGE"]
    assert held > 0
    # Clear the cooldown rather than move the clock: before_submit reads the
    # real wall clock and would reject a synthetic future quote.
    ledger.put("last_attempt", 0)
    plan = guard.plan("DOGE", "SELL", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    result = PaperBroker(s, ledger).execute(plan, guard.before_submit)
    assert result["status"] == "FILLED"
    assert ledger.positions["DOGE"] < held
    # Two fills, both charged.
    assert ledger.fees.accrued()["fills_charged"] == 2
    assert ledger.fees.outstanding("development") == ledger.fees.accrued()["dev_wei"]


def test_gas_is_charged_to_equity_but_not_to_cash(env):
    s, ledger, _ = env
    before_gas = ledger.gas_spent
    result = buy(env)
    assert int(result["gas_wei"]) > 0
    assert ledger.gas_spent > before_gas
    assert ledger.equity({"DOGE": quote()}) < D(s.capital)


def test_settlement_is_idempotent_and_immutable(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    cid = plan["client_order_id"]
    broker = PaperBroker(s, ledger)
    first = broker.execute(plan, guard.before_submit)
    cash = ledger.cash
    ledger.settle(
        cid,
        int(first["base_wei"]),
        int(first["quote_wei"]),
        int(first["fee_wei"]),
        int(first["gas_wei"]),
    )
    assert ledger.cash == cash
    with pytest.raises(RuntimeError, match="changed after finalization"):
        ledger.settle(cid, int(first["base_wei"]) + 1, int(first["quote_wei"]), 0, 0)


def test_settlement_refuses_a_fill_beyond_the_reserved_input(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    with pytest.raises(RuntimeError, match="spent more than the reserved input"):
        ledger.settle(
            plan["client_order_id"],
            int(plan["min_out_wei"]),
            int(plan["amount_in_wei"]) + 1,
            int(plan["planned_fee_wei"]),
        )


def test_settlement_refuses_a_fill_below_the_slippage_bound(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    with pytest.raises(RuntimeError, match="below the slippage bound"):
        ledger.settle(
            plan["client_order_id"],
            int(plan["min_out_wei"]) - 1,
            int(plan["amount_in_wei"]),
            int(plan["planned_fee_wei"]),
        )


def test_settlement_refuses_a_fee_that_does_not_match_the_accrual(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    with pytest.raises(RuntimeError, match="fee differs from the reserved fee"):
        ledger.settle(
            plan["client_order_id"],
            int(plan["min_out_wei"]),
            int(plan["amount_in_wei"]),
            int(plan["planned_fee_wei"]) + 1,
        )


def test_rejected_intent_cannot_settle(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    ledger.mark(plan["client_order_id"], "REJECTED")
    with pytest.raises(RuntimeError, match="rejected intent"):
        ledger.settle(plan["client_order_id"], 1, 1, 0)


def test_before_submit_rejection_marks_the_intent(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    guard.stop_file.write_text("")
    with pytest.raises(Veto):
        PaperBroker(s, ledger).execute(plan, guard.before_submit)
    assert ledger.order(plan["client_order_id"])["status"] == "REJECTED"


def test_reconcile_settles_an_interrupted_paper_fill(env):
    s, ledger, guard = env
    plan = guard.plan("DOGE", "BUY", {"DOGE": quote()})
    plan = ledger.reserve(plan, time.time())
    assert ledger.pending()
    PaperBroker(s, ledger).reconcile()
    assert not ledger.pending()
    assert ledger.fees.accrued()["fills_charged"] == 1


def test_settings_mismatch_refuses_to_reopen_a_ledger(tmp_path):
    a = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", a, "paper")
    ledger.close()
    with pytest.raises(RuntimeError, match="mismatch"):
        Ledger(tmp_path / "l.sqlite", Settings(order_limit="0.004"), "paper")


def test_mode_mismatch_refuses_to_reopen_a_ledger(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", s, "paper")
    ledger.close()
    with pytest.raises(RuntimeError, match="mismatch"):
        Ledger(tmp_path / "l.sqlite", s, "live")


# -- the action boundary ---------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"product": "DOGE", "side": "HOLD"},
        {"product": "DOGE"},
        {"product": "DOGE", "side": "BUY", "amount": "1"},
    ],
)
def test_proposal_schema_rejects_anything_but_a_side(args):
    with pytest.raises(ValidationError):
        Proposal.model_validate(args)


def test_provider_exposes_exactly_one_action(env):
    s, ledger, guard = env
    provider = StonkflyRHActions(guard, PaperBroker(s, ledger), "robinhood-mainnet")
    actions = provider.get_actions()
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


def test_provider_invoke_runs_the_guard_and_books_a_fee(env):
    s, ledger, guard = env
    provider = StonkflyRHActions(guard, PaperBroker(s, ledger), "robinhood-mainnet")
    provider.quotes = {"DOGE": quote()}
    result = provider.get_actions()[0].invoke({"product": "DOGE", "side": "BUY"})
    assert result["status"] == "FILLED"
    assert ledger.fees.accrued()["dev_wei"] > 0


# -- reinforcement ---------------------------------------------------------


@pytest.mark.parametrize(
    "equity,anchor,expected",
    [
        ("0.05", "0.04", "reward"),
        ("0.04", "0.05", "aversive"),
        ("0.05", "0.05", "none"),
    ],
)
def test_reinforcement_signs(equity, anchor, expected):
    kind, _ = reinforcement(equity, anchor, "0.00002")
    assert kind == expected
