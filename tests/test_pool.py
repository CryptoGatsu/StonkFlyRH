"""The donation pool: units, NAV, high-water marks and the 50/50 split.

The money-critical invariant is conservation: a deposit changes no one else's
value, and a settlement moves value only between the donor, their payout and
the operator, never out of thin air.
"""

import time

import pytest

from stonkflyrh.config import D, Settings
from stonkflyrh.ledger import Ledger
from stonkflyrh.pool import OPERATOR, Pool

ALICE = "0x" + "aa" * 20
BOB = "0x" + "bb" * 20


@pytest.fixture
def pool(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(donations_enabled=True), "paper", D("100"))
    p = Pool(ledger, "0.5")
    p.seed_operator(D("100"), 0)
    yield p, ledger
    ledger.close()


def value(pool, address, equity):
    p = pool.participant(address)
    return p["units"] * pool.nav(equity)


def close(a, b):
    # Units are exact Decimals but a settlement divides by NAV, which repeats.
    return abs(D(a) - D(b)) < D("0.000001")


def test_the_operator_starts_with_all_the_units_at_one_dollar(pool):
    p, _ = pool
    assert p.units_total() == D("100")
    assert p.nav(D("100")) == D(1)
    assert p.participant(OPERATOR)["units"] == D("100")


def test_a_deposit_buys_units_at_nav_and_moves_nobody_else(pool):
    p, _ = pool
    # The pool has made 20% before Alice arrives.
    assert p.nav(D("120")) == D("1.2")
    p.deposit(ALICE, D("60"), D("120"), 1)
    assert p.participant(ALICE)["units"] == D("50")
    # Operator's value is unchanged by Alice's arrival.
    assert value(p, OPERATOR, D("180")) == D("120")
    assert value(p, ALICE, D("180")) == D("60")


def test_a_donor_is_paid_half_the_gain_and_the_operator_gets_the_rest(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)          # NAV 1, 100 units
    equity = D("240")                                # NAV 1.2: Alice is up $20
    owed = p.due(equity, 2, "1")
    assert [o["address"] for o in owed] == [ALICE]
    assert owed[0]["gain"] == D("20") and owed[0]["payout"] == D("10")
    settled = p.settle(ALICE, equity, 2)
    assert settled["payout"] == D("10") and settled["fee"] == D("10")
    after = equity - settled["payout"]                # the $10 has left the wallet
    # Alice is back at her high-water value; the fee arrived as operator units.
    assert close(value(p, ALICE, after), "100")
    assert close(value(p, OPERATOR, after), "130")   # 120 of own gain + 10 fee
    # Nothing was created: donor value + operator value == what is left.
    assert close(value(p, ALICE, after) + value(p, OPERATOR, after), after)


def test_nav_is_unchanged_once_the_payout_has_left(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)
    before = p.nav(D("240"))
    settled = p.settle(ALICE, D("240"), 2)
    assert close(p.nav(D("240") - settled["payout"]), before)


def test_no_payout_below_the_high_water_mark(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)
    assert p.due(D("180"), 2, "1") == []            # NAV 0.9: Alice is down
    assert p.settle(ALICE, D("180"), 2) is None


def test_losses_must_be_recovered_before_the_next_payout(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)
    p.settle(ALICE, D("240"), 2)                     # crest at NAV 1.2
    assert p.due(D("220"), 3, "1") == []            # NAV 1.1: below Alice's mark
    assert p.due(D("260"), 4, "1")[0]["gain"] > 0   # NAV 1.3: above it


def test_the_operator_is_never_paid_out(pool):
    p, _ = pool
    assert p.due(D("150"), 1, "1") == []


def test_a_second_deposit_blends_the_high_water_mark(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)          # 100 units at 1.0
    p.deposit(ALICE, D("120"), D("240"), 2)          # 100 units at 1.2
    a = p.participant(ALICE)
    assert a["units"] == D("200")
    assert a["hwm"] == D("1.1")
    # New money is not charged for the old gain: at NAV 1.2 only the first
    # tranche's $20 is a gain.
    assert p.due(D("360"), 3, "1")[0]["gain"] == D("20")


def test_tiny_gains_wait_for_the_minimum(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)
    assert p.due(D("201"), 2, "1") == []            # gain 50 cents, payout 25c
    assert p.due(D("206"), 2, "1")                  # gain $3, payout $1.50


def test_a_deposit_is_booked_once_per_transfer(pool):
    p, _ = pool
    assert p.deposit(ALICE, D("10"), D("100"), 1, "0xabc", 0) is not None
    assert p.deposit(ALICE, D("10"), D("100"), 1, "0xabc", 0) is None
    assert p.participant(ALICE)["deposited"] == D("10")


def test_two_donors_are_settled_independently(pool):
    p, _ = pool
    p.deposit(ALICE, D("100"), D("100"), 1)          # NAV 1
    p.deposit(BOB, D("120"), D("240"), 2)            # NAV 1.2
    equity = D("390")                                # NAV 1.3
    owed = {o["address"]: o for o in p.due(equity, 3, "1")}
    assert owed[ALICE]["gain"] == D("30")            # 100 units × 0.3
    assert owed[BOB]["gain"] == D("10")              # 100 units × 0.1


def test_the_report_lists_everyone(pool):
    p, _ = pool
    p.deposit(ALICE, D("50"), D("100"), 1)
    report = p.report(D("165"))
    assert report["donors"] == 1
    rows = {r["address"]: r for r in report["participants"]}
    assert rows[ALICE]["value"] == "55.00" and rows[ALICE]["unrealised"] == "5.00"
    assert rows[OPERATOR]["value"] == "110.00"


# -- the ledger side -----------------------------------------------------------


def test_a_ledger_deposit_is_not_profit(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", D("100"))
    try:
        ledger.deposit(D("25"), 1)
        assert ledger.cash == D("125")
        assert D(ledger.get("initial_cash")) == D("125")
        assert D(ledger.get("anchor")) == D("125")     # no reward pulse from a gift
        assert D(ledger.get("deposited_total")) == D("25")
    finally:
        ledger.close()


def test_a_ledger_withdrawal_needs_the_cash(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite", Settings(), "paper", D("100"))
    try:
        ledger.withdraw(D("10"), 1)
        assert ledger.cash == D("90") and D(ledger.get("anchor")) == D("90")
        with pytest.raises(RuntimeError, match="exceeds cash"):
            ledger.withdraw(D("1000"), 2)
    finally:
        ledger.close()


def test_the_loss_stop_scales_with_the_pool(tmp_path):
    from stonkflyrh.risk import Guard, Veto
    from tests.test_execution import ETH_USD, quote

    s = Settings()
    ledger = Ledger(tmp_path / "l.sqlite", s, "paper", D("100"))
    try:
        guard = Guard(s, ledger, tmp_path / "STOP")
        ledger.deposit(D("900"), 1)                     # a $1,000 pool
        ledger.put("cash", "960")                        # down $40: a $25 stop would fire
        guard.check({"PONS": quote()}, time.time(), ETH_USD)
        ledger.put("cash", "740")                        # down 26%: the fractional stop fires
        with pytest.raises(Veto, match="Loss stop"):
            guard.check({"PONS": quote()}, time.time(), ETH_USD)
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "changes",
    [dict(donor_share="1.5"), dict(donor_payout_interval_seconds=60),
     dict(max_pool_usd="50"), dict(loss_stop_fraction="1")],
)
def test_donation_settings_bounds(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


# -- money that arrived before the run ----------------------------------------


def test_the_operator_stake_follows_the_real_starting_balance(tmp_path):
    """A live ledger is seeded with the capital cap and only learns the wallet's
    balance at preflight; the operator must not be left holding cap-sized units."""
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(donations_enabled=True), "live", D("110"))
    try:
        p = Pool(ledger, "0.5")
        p.seed_operator(D("110"), 0)
        p.seed_operator(D("100.078"), 1)             # preflight found 100.078 USDG
        assert p.participant(OPERATOR)["units"] == D("100.078")
        assert p.nav(D("100.078")) == D(1)
        # Once anyone else holds units the stake is history, not a placeholder.
        p.deposit(ALICE, D("10"), D("100.078"), 2)
        p.seed_operator(D("50"), 3)
        assert p.participant(OPERATOR)["units"] == D("100.078")
    finally:
        ledger.close()


def test_reassigning_a_pre_run_transfer_moves_value_not_units_total(pool):
    p, _ = pool
    # Alice's 10 USDG arrived before the run and sits inside the operator's 100.
    before = p.units_total()
    entry = p.reassign(ALICE, D("10"), D("100"), 1, "0xabc", 0, 90)
    assert entry["units"] == D("10")
    assert p.units_total() == before
    assert value(p, ALICE, D("100")) == D("10")
    assert value(p, OPERATOR, D("100")) == D("90")
    assert p.participant(OPERATOR)["deposited"] == D("90")
    # Booking the same transfer again does nothing.
    assert p.reassign(ALICE, D("10"), D("100"), 2, "0xabc", 0, 90) is None
    assert value(p, ALICE, D("100")) == D("10")
    # And Alice is paid on gains above the NAV she was booked at.
    owed = p.due(D("120"), 3, D(0))
    assert [o["address"] for o in owed] == [ALICE] and owed[0]["gain"] == D("2")


def test_reassigning_more_than_the_operator_holds_is_refused(pool):
    p, _ = pool
    with pytest.raises(ValueError, match="does not cover"):
        p.reassign(ALICE, D("500"), D("100"), 1, "0xdef")
