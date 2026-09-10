"""The 20% development split is the money-critical invariant of this fork."""

import pytest

from stonkflyrh.config import Settings
from stonkflyrh.fees import (
    BPS_DENOMINATOR,
    DEV_SHARE_BPS,
    FeeBook,
    gross_fee_wei,
    split_fee,
)
from stonkflyrh.ledger import Ledger


@pytest.fixture
def book(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper")
    yield FeeBook(ledger), ledger
    ledger.close()


def test_dev_share_is_twenty_percent():
    assert DEV_SHARE_BPS == 2000
    assert DEV_SHARE_BPS / BPS_DENOMINATOR == 0.2


@pytest.mark.parametrize(
    "gross", [0, 1, 4, 5, 9, 99, 100, 101, 999, 10**9 + 7, 10**18, 3 * 10**17 - 1]
)
def test_split_conserves_and_never_overpays(gross):
    dev, treasury = split_fee(gross)
    assert dev + treasury == gross
    assert dev >= 0 and treasury >= 0
    # Rounding always favours the treasury, so a payout can never exceed intake.
    assert dev <= gross * DEV_SHARE_BPS // BPS_DENOMINATOR
    assert dev * BPS_DENOMINATOR <= gross * DEV_SHARE_BPS


def test_split_matches_twenty_percent_on_round_numbers():
    assert split_fee(10**18) == (2 * 10**17, 8 * 10**17)
    assert split_fee(1000) == (200, 800)


def test_split_rejects_negative():
    with pytest.raises(ValueError):
        split_fee(-1)


@pytest.mark.parametrize("bps", [-1, BPS_DENOMINATOR + 1])
def test_fee_basis_bounds(bps):
    with pytest.raises(ValueError):
        gross_fee_wei(10**18, bps)


def test_accrual_is_idempotent(book):
    fees, _ = book
    first = fees.accrue("order-1", 10**18, 100, 0)
    second = fees.accrue("order-1", 10**18, 100, 5)
    assert first == second
    assert fees.accrued()["fills_charged"] == 1
    assert first["dev_wei"] == 2 * 10**15
    assert first["treasury_wei"] == 8 * 10**15


def test_reaccrual_with_different_amounts_is_refused(book):
    fees, _ = book
    fees.accrue("order-1", 10**18, 100, 0)
    with pytest.raises(RuntimeError):
        fees.accrue("order-1", 2 * 10**18, 100, 0)


def test_totals_and_outstanding_track_payouts(book):
    fees, _ = book
    for i in range(5):
        fees.accrue(f"order-{i}", 10**18, 100, i)
    totals = fees.accrued()
    assert totals["gross_wei"] == 5 * 10**16
    assert totals["dev_wei"] == 10**16
    assert totals["dev_wei"] + totals["treasury_wei"] == totals["gross_wei"]
    assert fees.outstanding("development") == 10**16

    payout = fees.record_payout("development", "0x" + "11" * 20, 4 * 10**15, 0)
    fees.mark_payout(payout, "SENT", "0xabc")
    assert fees.outstanding("development") == 6 * 10**15
    # A rejected payout returns to outstanding rather than vanishing.
    rejected = fees.record_payout("development", "0x" + "11" * 20, 10**15, 0)
    fees.mark_payout(rejected, "REJECTED")
    assert fees.outstanding("development") == 6 * 10**15


def test_outstanding_never_goes_negative(book):
    fees, _ = book
    fees.accrue("order-1", 10**18, 100, 0)
    payout = fees.record_payout("development", "0x" + "11" * 20, 10**18, 0)
    fees.mark_payout(payout, "SENT", "0xdead")
    assert fees.outstanding("development") == 0


def test_fee_tables_survive_reopen(tmp_path):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    FeeBook(ledger).accrue("order-1", 10**18, 100, 0)
    ledger.close()
    reopened = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    try:
        assert FeeBook(reopened).accrued()["dev_wei"] == 2 * 10**15
    finally:
        reopened.close()


def test_dev_share_is_recorded_on_every_row(book):
    fees, ledger = book
    fees.accrue("order-1", 10**18, 100, 0)
    rows = ledger.db.execute("SELECT dev_share_bps FROM fees").fetchall()
    assert [r[0] for r in rows] == [2000]


def test_report_reflects_configured_dev_wallet(book, monkeypatch):
    fees, _ = book
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", "0x" + "ab" * 20)
    fees.accrue("order-1", 10**18, 100, 0)
    report = fees.report()
    assert report["dev_share_percent"] == 20.0
    assert report["dev_wallet"].lower() == "0x" + "ab" * 20
    assert report["development"] == "0.002"
