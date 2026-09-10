"""The live broker's accounting and the fee sweeper, on in-memory doubles.

Nothing here signs, broadcasts or contacts an endpoint. What is exercised is
the part that decides how much was traded and how much is owed.
"""

import time

import pytest

from stonkflyrh.broker import RobinhoodChainBroker, UnresolvedOrder
from stonkflyrh.config import D, QUOTE_DECIMALS, Settings, to_wei
from stonkflyrh.ledger import Ledger
from stonkflyrh.payouts import FeeSweeper
from stonkflyrh.risk import Guard
from tests.test_execution import CAPITAL, ETH_USD, quote

WETH = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
TRADER = "0x" + "33" * 20
FEE_WALLET = "0x" + "44" * 20


class Held:
    def __init__(self, value):
        self.value = value

    def call(self, *_a, **_k):
        return self.value


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
        token = "base" if address.lower() == TOKEN.lower() else "quote"
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
        return {"symbol": symbol, "address": TOKEN, "decimals": 18}

    def pool_fee(self, _symbol, default):
        return default


class FakeAccount:
    address = TRADER


def broker(tmp_path, client=None, settings=None):
    settings = settings or Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live", CAPITAL)
    client = client or FakeClient()
    b = RobinhoodChainBroker(
        settings, ledger, client, FakeRegistry(), {}, FakeAccount(), FEE_WALLET
    )
    return b, ledger, Guard(settings, ledger, tmp_path / "STOP")


def plan_for(guard, ledger, side, quotes=None):
    quotes = quotes or {"PONS": quote()}
    return ledger.reserve(guard.plan("PONS", side, quotes, ETH_USD), time.time())


def receipt(gas_used=200000, price=10**8):
    return {
        "tx_hash": "0x" + "ab" * 32,
        "status": 1,
        "gas_used": gas_used,
        "effective_gas_price": price,
        "block": 42,
    }


# -- leg resolution --------------------------------------------------------


def test_buy_spends_weth_and_receives_the_memecoin(tmp_path):
    b, ledger, guard = broker(tmp_path)
    try:
        p = plan_for(guard, ledger, "BUY")
        assert b.token_address(p, "in") == WETH
        assert b.token_address(p, "out") == TOKEN
    finally:
        ledger.close()


def test_sell_spends_the_memecoin_and_receives_weth(tmp_path):
    b, ledger, guard = broker(tmp_path)
    try:
        ledger.put("positions", {"PONS": "40000"})
        p = plan_for(guard, ledger, "SELL")
        assert b.token_address(p, "in") == TOKEN
        assert b.token_address(p, "out") == WETH
    finally:
        ledger.close()


# -- booking a mined swap --------------------------------------------------


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
        assert ledger.positions["PONS"] > 0
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
        assert to_wei(ledger.positions["PONS"], 18) == arrived
    finally:
        ledger.close()


def test_sell_fee_is_taken_from_realised_proceeds(tmp_path):
    settings = Settings(protocol_fee_bps=100)
    client = FakeClient()
    b, ledger, guard = broker(tmp_path, client, settings)
    try:
        ledger.put("positions", {"PONS": "40000"})
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
    b, ledger, guard = broker(tmp_path, client, Settings(protocol_fee_bps=100))
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
    client = FakeClient(quote_wei=to_wei(CAPITAL, QUOTE_DECIMALS))
    b, ledger, _ = broker(tmp_path, client)
    try:
        b.verify_balances()
        client.balances["quote"] += 10**15
        with pytest.raises(RuntimeError, match="does not match the ledger"):
            b.verify_balances()
    finally:
        ledger.close()


def test_live_preflight_needs_the_price_reference(tmp_path):
    client = FakeClient(quote_wei=to_wei(CAPITAL, QUOTE_DECIMALS))
    b, ledger, _ = broker(tmp_path, client)
    try:
        with pytest.raises(RuntimeError, match="ETH/USD reference"):
            b.preflight()
    finally:
        ledger.close()


def test_live_preflight_caps_funding_at_the_dollar_limit(tmp_path):
    # $150 in the wallet against a $100 configured stake.
    client = FakeClient(quote_wei=to_wei(D("150") / ETH_USD, QUOTE_DECIMALS))
    b, ledger, _ = broker(tmp_path, client)
    try:
        with pytest.raises(RuntimeError, match=r"\$100 cap"):
            b.preflight(ETH_USD)
    finally:
        ledger.close()


# -- fee payouts -----------------------------------------------------------


def sweeper(tmp_path, client=None, destination=FEE_WALLET):
    settings = Settings(protocol_fee_bps=100)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live", CAPITAL)
    return (
        FeeSweeper(
            settings, ledger, client or FakeClient(), FakeRegistry(), FakeAccount(), destination
        ),
        ledger,
    )


def test_the_destination_is_the_configured_fee_wallet(tmp_path):
    s, ledger = sweeper(tmp_path)
    try:
        assert s.destination() == FEE_WALLET
    finally:
        ledger.close()


def test_sweeping_to_the_fly_wallet_itself_is_refused(tmp_path):
    s, ledger = sweeper(tmp_path, destination=TRADER)
    try:
        with pytest.raises(RuntimeError, match="only pay gas"):
            s.destination()
    finally:
        ledger.close()


def test_dry_run_reports_what_is_owed_without_sending(tmp_path):
    client = FakeClient(quote_wei=10**18)
    s, ledger = sweeper(tmp_path, client)
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)  # 0.01 WETH of fee
        row = s.sweep(dry_run=True)["payouts"][0]
        assert row["status"] == "WOULD_SEND"
        assert row["destination"] == FEE_WALLET
        assert row["outstanding_wei"] == 10**16
        assert not ledger.fees.payouts()  # a dry run records nothing
    finally:
        ledger.close()


def test_a_sweep_below_the_threshold_is_skipped(tmp_path):
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=10**18))
    try:
        ledger.fees.accrue("order-1", 10**12, 100, 0)
        assert s.sweep()["payouts"][0]["status"] == "BELOW_THRESHOLD"
    finally:
        ledger.close()


def test_a_sweep_never_sends_more_than_the_wallet_holds(tmp_path):
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=0))
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)
        assert s.sweep()["payouts"][0]["status"] == "INSUFFICIENT_BALANCE"
    finally:
        ledger.close()


def test_an_interrupted_payout_is_reconciled_not_resent(tmp_path):
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=10**18))
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)
        ledger.fees.record_payout(FEE_WALLET, 10**15, 0)
        s.reconcile()
        # Nothing is broadcast before a hash is recorded, so PREPARED is a no-op.
        assert ledger.fees.outstanding() == 10**16
        assert {r["status"] for r in ledger.fees.payouts()} == {"REJECTED"}
    finally:
        ledger.close()


def test_a_payout_stuck_without_a_hash_refuses_to_sweep_again(tmp_path):
    s, ledger = sweeper(tmp_path, FakeClient(quote_wei=10**18))
    try:
        ledger.fees.accrue("order-1", 10**18, 100, 0)
        stuck = ledger.fees.record_payout(FEE_WALLET, 10**15, 0)
        ledger.fees.mark_payout(stuck, "UNKNOWN")
        with pytest.raises(RuntimeError, match="no transaction hash"):
            s.sweep()
    finally:
        ledger.close()
