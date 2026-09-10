"""Fee accrual and payout bookkeeping."""

import pytest

from stonkflyrh.config import D, Settings
from stonkflyrh.fees import BPS_DENOMINATOR, FeeBook, fee_wallet, gross_fee_wei
from stonkflyrh.ledger import Ledger

CAPITAL = D("100")


@pytest.fixture
def book(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", Settings(), "paper", CAPITAL)
    yield FeeBook(ledger), ledger
    ledger.close()


def test_the_default_run_charges_no_protocol_fee():
    # Both wallets belong to the operator, so a fee would only pay gas to move
    # their own money. It stays available for a run where that is not true.
    assert Settings().protocol_fee_bps == 0


@pytest.mark.parametrize("bps", [-1, BPS_DENOMINATOR + 1])
def test_fee_basis_bounds(bps):
    with pytest.raises(ValueError):
        gross_fee_wei(10**18, bps)


@pytest.mark.parametrize(
    "quote,bps,expected",
    [(10**18, 100, 10**16), (10**18, 0, 0), (999, 100, 9), (1, 100, 0)],
)
def test_fee_rounds_down(quote, bps, expected):
    assert gross_fee_wei(quote, bps) == expected


def test_accrual_is_idempotent(book):
    fees, _ = book
    first = fees.accrue("order-1", 10**18, 100, 0)
    assert fees.accrue("order-1", 10**18, 100, 5) == first
    assert fees.accrued() == {"fills_charged": 1, "gross_wei": 10**16}


def test_reaccrual_with_different_amounts_is_refused(book):
    fees, _ = book
    fees.accrue("order-1", 10**18, 100, 0)
    with pytest.raises(RuntimeError, match="changed after it was booked"):
        fees.accrue("order-1", 2 * 10**18, 100, 0)


def test_outstanding_tracks_payouts(book):
    fees, _ = book
    for i in range(5):
        fees.accrue(f"order-{i}", 10**18, 100, i)
    assert fees.outstanding() == 5 * 10**16
    payout = fees.record_payout("0x" + "11" * 20, 2 * 10**16, 0)
    fees.mark_payout(payout, "SENT", "0xabc")
    assert fees.outstanding() == 3 * 10**16
    # A rejected payout returns to outstanding rather than vanishing.
    rejected = fees.record_payout("0x" + "11" * 20, 10**16, 0)
    fees.mark_payout(rejected, "REJECTED")
    assert fees.outstanding() == 3 * 10**16


def test_outstanding_never_goes_negative(book):
    fees, _ = book
    fees.accrue("order-1", 10**18, 100, 0)
    payout = fees.record_payout("0x" + "11" * 20, 10**18, 0)
    fees.mark_payout(payout, "SENT", "0xdead")
    assert fees.outstanding() == 0


def test_fee_tables_survive_reopen(tmp_path):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper", CAPITAL)
    FeeBook(ledger).accrue("order-1", 10**18, 100, 0)
    ledger.close()
    reopened = Ledger(tmp_path / "ledger.sqlite", settings, "paper")
    try:
        assert FeeBook(reopened).accrued()["gross_wei"] == 10**16
    finally:
        reopened.close()


def test_fee_wallet_defaults_to_the_operators_address(monkeypatch):
    monkeypatch.delenv("STONKFLYRH_FEE_WALLET", raising=False)
    assert fee_wallet().lower() == "0x7f5afc67d4c3ae0182354ea6e785fdeb20150f15"


def test_fee_wallet_can_be_overridden(monkeypatch):
    monkeypatch.setenv("STONKFLYRH_FEE_WALLET", "0x" + "ab" * 20)
    assert fee_wallet().lower() == "0x" + "ab" * 20


def test_report_carries_the_destination(book, monkeypatch):
    fees, _ = book
    monkeypatch.setenv("STONKFLYRH_FEE_WALLET", "0x" + "ab" * 20)
    fees.accrue("order-1", 10**18, 100, 0)
    report = fees.report(quote_decimals=18)
    assert report["gross"] == "0.01"
    assert report["fee_wallet"].lower() == "0x" + "ab" * 20
    assert report["unpaid_wei"] == 10**16
