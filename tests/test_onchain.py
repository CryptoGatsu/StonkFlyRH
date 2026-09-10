"""The live broker's accounting, driven by in-memory chain doubles.

Nothing here signs, broadcasts or contacts an endpoint. What is exercised is the
part that decides how much was traded and how much of the fee is owed to whom.
"""

import time

import pytest

from stonkflyrh.broker import RobinhoodChainBroker, UnresolvedOrder
from stonkflyrh.config import D, QUOTE_DECIMALS, Settings, to_wei
from stonkflyrh.fees import split_fee
from stonkflyrh.ledger import Ledger
from stonkflyrh.payouts import FeeSweeper
from stonkflyrh.risk import Guard
from tests.test_execution import quote

WETH = "0x" + "11" * 20
DOGE = "0x" + "22" * 20
TRADER = "0x" + "33" * 20
FEE_WALLET = "0x" + "44" * 20
DEV_WALLET = "0x" + "de" * 20


class Balances:
    def __init__(self, owner, token):
        self.owner = owner
        self.token = token

    def balanceOf(self, _address):
        return Held(self.owner.balances[self.token])

    def allowance(self, *_):
        return Held(2**255)

    def factory(self):
        return Held(WETH)


class Held:
    def __init__(self, value):
        self.value = value

    def call(self, *_a, **_k):
        return self.value


class FakeClient:
    net = type(
        "Net",
        (),
        {
            "name": "Robinhood Chain",
            "chain_id": 4663,
            "tx_url": staticmethod(lambda h: "https://explorer.example/tx/" + h),
        },
    )()

    def __init__(self, quote_wei=0, base_wei=0, gas_wei=10**16):
        self.balances = {"quote": quote_wei, "base": base_wei, "gas": gas_wei}

    def contract(self, address, abi):
        token = "base" if address.lower() == DOGE.lower() else "quote"
        return type("C", (), {"functions": Balances(self, token)})()

    def erc20(self, address):
        return self.contract(address, None)

    def balance(self, _address):
        return self.balances["gas"]

    def gas_price(self):
        return 10**8


class FakeRegistry:
    quote_address = WETH
    router = "0x" + "55" * 20
    quoter = "0x" + "66" * 20

    def token(self, symbol):
        return {"symbol": symbol, "address": DOGE, "decimals": 18}

    def pool_fee(self, symbol, default):
        return default


class FakeAccount:
    address = TRADER


def broker(tmp_path, client=None, settings=None):
    settings = settings or Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live")
    client = client or FakeClient()
    b = RobinhoodChainBroker(
        settings, ledger, client, FakeRegistry(), {}, FakeAccount(), FEE_WALLET
    )
    return b, ledger, Guard(settings, ledger, tmp_path / "STOP")


def plan_for(guard, ledger, side, quotes=None):
    quotes = quotes or {"DOGE": quote()}
    return ledger.reserve(guard.plan("DOGE", side, quotes), time.time())


# -- leg resolution --------------------------------------------------------


def test_buy_spends_weth_and_receives_the_memecoin(tmp_path):
    b, ledger, guard = broker(tmp_path)
    try:
        p = plan_for(guard, ledger, "BUY")
        assert b.token_address(p, "in") == WETH
        assert b.token_address(p, "out") == DOGE
    finally:
        ledger.close()


def test_sell_spends_the_memecoin_and_receives_weth(tmp_path):
    b, ledger, guard = broker(tmp_path)
    try:
        ledger.put("positions", {"DOGE": "50000"})
        p = plan_for(guard, ledger, "SELL")
        assert b.token_address(p, "in") == DOGE
        assert b.token_address(p, "out") == WETH
    finally:
        ledger.close()


# -- booking a mined swap --------------------------------------------------


def receipt(gas_used=200000, price=10**8):
    return {
        "tx_hash": "0x" + "ab" * 32,
        "status": 1,
        "gas_used": gas_used,
        "effective_gas_price": price,
        "block": 42,
    }


def test_buy_is_booked_from_balance_deltas(tmp_path):
    client = FakeClient()
    b, ledger, guard = broker(tmp_path, client)
    try:
        p = plan_for(guard, ledger, "BUY")
        spent = int(p["amount_in_wei"])
        received = int(p["min_out_wei"]) + 10**18
        before = {"quote": spent, "base": 0, "gas": 10**16}
        client.balances = {"quote": 0, "base": received, "gas": 10**16}
        result = b._book(p["client_order_id"], p, receipt(), before)
        assert result["status"] == "SETTLED"
        assert int(result["base_wei"]) == received
        assert int(result["quote_wei"]) == spent
        assert int(result["fee_wei"]) == int(p["planned_fee_wei"])
        assert int(result["dev_fee_wei"]) == int(result["fee_wei"]) * 2 // 10
        assert int(result["dev_fee_wei"]) + int(result["treasury_fee_wei"]) == int(
            result["fee_wei"]
        )
        assert ledger.positions["DOGE"] > 0
    finally:
        ledger.close()


def test_a_transfer_taxing_token_books_what_actually_arrived(tmp_path):
    """Balance deltas, not the router's return value, decide the position."""
    client = FakeClient()
    b, ledger, guard = broker(tmp_path, client)
    try:
        p = plan_for(guard, ledger, "BUY")
        arrived = int(p["min_out_wei"])  # exactly the bound, after a transfer tax
        before = {"quote": int(p["amount_in_wei"]), "base": 0, "gas": 10**16}
        client.balances = {"quote": 0, "base": arrived, "gas": 10**16}
        b._book(p["client_order_id"], p, receipt(), before)
        assert to_wei(ledger.positions["DOGE"], 18) == arrived
    finally:
        ledger.close()


def test_sell_fee_is_taken_from_realised_proceeds(tmp_path):
    client = FakeClient()
    settings = Settings()
    b, ledger, guard = broker(tmp_path, client, settings)
    try:
        ledger.put("positions", {"DOGE": "50000"})
        p = plan_for(guard, ledger, "SELL")
        sold = int(p["amount_in_wei"])
        proceeds = int(p["min_out_wei"]) + 10**12
        before = {"quote": 0, "base": sold, "gas": 10**16}
        client.balances = {"quote": proceeds, "base": 0, "gas": 10**16}
        result = b._book(p["client_order_id"], p, receipt(), before)
        expected_fee = proceeds * settings.protocol_fee_bps // 10000
        assert int(result["fee_wei"]) == expected_fee
        # Realised proceeds beat the plan's estimate, so the fee did too.
        assert expected_fee != int(p["planned_fee_wei"])
        assert int(result["dev_fee_wei"]) == split_fee(expected_fee)[0]
    finally:
        ledger.close()


def test_approval_gas_is_added_to_the_swap(tmp_path):
    client = FakeClient()
    b, ledger, guard = broker(tmp_path, client)
    try:
        p = plan_for(guard, ledger, "BUY")
        before = {"quote": int(p["amount_in_wei"]), "base": 0, "gas": 10**16}
        client.balances = {"quote": 0, "base": int(p["min_out_wei"]) + 1, "gas": 10**16}
        result = b._book(p["client_order_id"], p, receipt(), before, approval_gas=5000)
        assert int(result["gas_wei"]) == 200000 * 10**8 + 5000
    finally:
        ledger.close()


def test_a_swap_that_moved_nothing_is_unresolved(tmp_path):
    client = FakeClient()
    b, ledger, guard = broker(tmp_path, client)
    try:
        p = plan_for(guard, ledger, "BUY")
        before = {"quote": int(p["amount_in_wei"]), "base": 0, "gas": 10**16}
        client.balances = dict(before)
        with pytest.raises(UnresolvedOrder, match="balances did not move"):
            b._book(p["client_order_id"], p, receipt(), before)
    finally:
        ledger.close()


# -- balance reconciliation ------------------------------------------------


def test_unswept_fees_are_expected_to_sit_in_the_wallet(tmp_path):
    client = FakeClient()
    b, ledger, guard = broker(tmp_path, client)
    try:
        p = plan_for(guard, ledger, "BUY")
        before = {"quote": int(p["amount_in_wei"]), "base": 0, "gas": 10**16}
        client.balances = {"quote": 0, "base": int(p["min_out_wei"]) + 1, "gas": 10**16}
        b._book(p["client_order_id"], p, receipt(), before)
        assert b.unswept_fees() == int(p["planned_fee_wei"])
        expected = b.expected()
        assert expected["quote"] == to_wei(ledger.cash, QUOTE_DECIMALS) + b.unswept_fees()
    finally:
        ledger.close()


def test_an_external_transfer_stops_the_run(tmp_path):
    client = FakeClient(quote_wei=to_wei("0.05", QUOTE_DECIMALS))
    b, ledger, _ = broker(tmp_path, client)
    try:
        b.verify_balances()
        client.balances["quote"] += 10**15
        with pytest.raises(RuntimeError, match="does not match the ledger"):
            b.verify_balances()
    finally:
        ledger.close()


# -- fee payouts -----------------------------------------------------------


def sweeper(tmp_path, client=None):
    settings = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live")
    return (
        FeeSweeper(
            settings,
            ledger,
            client or FakeClient(),
            FakeRegistry(),
            FakeAccount(),
            FEE_WALLET,
        ),
        ledger,
    )


def test_development_destination_requires_a_configured_wallet(tmp_path, monkeypatch):
    monkeypatch.delenv("STONKFLYRH_DEV_WALLET", raising=False)
    s, ledger = sweeper(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="nowhere to go"):
            s.destination("development")
        assert s.destination("treasury") == FEE_WALLET
    finally:
        ledger.close()


def test_dry_run_reports_the_twenty_eighty_split_without_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", DEV_WALLET)
    client = FakeClient(quote_wei=10**18)
    s, ledger = sweeper(tmp_path, client)
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)  # 0.01 WETH of fee
        report = s.sweep(dry_run=True)
        assert report["dev_share_percent"] == 20.0
        by_name = {p["beneficiary"]: p for p in report["payouts"]}
        assert by_name["development"]["status"] == "WOULD_SEND"
        assert by_name["development"]["destination"].lower() == DEV_WALLET
        assert by_name["development"]["outstanding_wei"] == 2 * 10**15
        assert by_name["treasury"]["outstanding_wei"] == 8 * 10**15
        assert not ledger.fees.payouts()  # a dry run records nothing
    finally:
        ledger.close()


def test_a_sweep_below_the_threshold_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", DEV_WALLET)
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=10**18))
    try:
        ledger.fees.accrue("order-1", 10**12, 100, 0)
        statuses = {p["beneficiary"]: p["status"] for p in s.sweep()["payouts"]}
        assert set(statuses.values()) == {"BELOW_THRESHOLD"}
    finally:
        ledger.close()


def test_a_sweep_never_sends_more_than_the_wallet_holds(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", DEV_WALLET)
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=0))
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)
        statuses = {p["beneficiary"]: p["status"] for p in s.sweep()["payouts"]}
        assert statuses["development"] == "INSUFFICIENT_BALANCE"
    finally:
        ledger.close()


def test_an_interrupted_payout_is_reconciled_not_resent(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", DEV_WALLET)
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=10**18))
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)
        prepared = ledger.fees.record_payout("development", DEV_WALLET, 10**15, 0)
        s.reconcile()
        # Nothing is broadcast before a hash is recorded, so PREPARED is a no-op.
        assert ledger.fees.outstanding("development") == 2 * 10**15
        rows = {r["status"] for r in ledger.fees.payouts()}
        assert rows == {"REJECTED"}
        assert prepared
    finally:
        ledger.close()


def test_a_payout_stuck_without_a_hash_refuses_to_sweep_again(tmp_path, monkeypatch):
    monkeypatch.setenv("STONKFLYRH_DEV_WALLET", DEV_WALLET)
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=10**18))
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)
        stuck = ledger.fees.record_payout("development", DEV_WALLET, 10**15, 0)
        ledger.fees.mark_payout(stuck, "UNKNOWN")
        with pytest.raises(RuntimeError, match="no transaction hash"):
            s.sweep()
    finally:
        ledger.close()
