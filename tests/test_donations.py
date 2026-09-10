"""Recognising donations from USDG Transfer logs, and paying donors.

Chain calls here are in-memory doubles. No transfer is signed or sent.
"""

import time


from stonkflyrh.config import D, Settings, to_wei
from stonkflyrh.donations import Donations, FixtureDonations, transfer_topic
from stonkflyrh.ledger import Ledger
from stonkflyrh.pool import OPERATOR, Pool
from tests.test_execution import ETH_USD, quote

USDG = "0x" + "11" * 20
FLY = "0x" + "33" * 20
ALICE = "0x" + "aa" * 20
POOL_ADDR = "0x" + "99" * 20


def pad(addr):
    return bytes(12) + bytes.fromhex(addr[2:])


def transfer_log(sender, amount_usdg, tx, block=100, index=0):
    return {
        "topics": [transfer_topic(), pad(sender), pad(FLY)],
        "data": to_wei(amount_usdg, 6).to_bytes(32, "big"),
        "transactionHash": tx,
        "blockNumber": block,
        "logIndex": index,
    }


class Eth:
    def __init__(self, logs, head):
        self.logs = logs
        self.block_number = head

    def get_logs(self, q):
        return [l for l in self.logs if q["fromBlock"] <= l["blockNumber"] <= q["toBlock"]]


class Client:
    net = type("Net", (), {"chain_id": 4663, "tx_url": staticmethod(lambda h: h)})()

    def __init__(self, logs, head=1000):
        self.w3 = type("W3", (), {})()
        self.w3.eth = Eth(logs, head)

    def erc20(self, _address):
        return None

    def logs(self, params, chunk=2000, pause=0, retries=6, max_blocks=None):
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        if max_blocks is not None:
            hi = min(hi, lo + int(max_blocks) - 1)
        return self.w3.eth.get_logs({**params, "fromBlock": lo, "toBlock": hi}), hi


class Registry:
    quote_address = USDG
    quote_decimals = 6


def build(tmp_path, logs, **overrides):
    settings = Settings(donations_enabled=True, **overrides)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live", D("100"))
    pool = Pool(ledger, settings.donor_share)
    pool.seed_operator(D("100"), 0)
    client = Client(logs)
    d = Donations(settings, ledger, pool, client, Registry(), None, FLY)
    ledger.put("donations_block", 50)
    return settings, ledger, pool, d


def test_an_inbound_transfer_becomes_a_deposit_at_nav(tmp_path):
    _, ledger, pool, d = build(tmp_path, [transfer_log(ALICE, "25", "0xgift")])
    try:
        booked = d.ingest(time.time(), ETH_USD, {"PONS": quote()})
        assert len(booked) == 1 and booked[0]["credited_to"] == ALICE
        assert ledger.cash == D("125")
        assert D(ledger.get("anchor")) == D("125")      # a gift is not a reward
        assert pool.participant(ALICE)["units"] == D("25")   # NAV was 1.0
        assert ledger.get("donations_block") == 1000
    finally:
        ledger.close()


def test_our_own_swap_proceeds_are_not_donations(tmp_path):
    _, ledger, pool, d = build(tmp_path, [transfer_log(POOL_ADDR, "9.9", "0xswap1")])
    try:
        ledger.db.execute(
            "INSERT INTO orders(id,status,created,plan,exchange_id) VALUES (?,?,?,?,?)",
            ("o1", "SETTLED", 0, "{}", "0xswap1"),
        )
        assert d.ingest(time.time(), ETH_USD, {"PONS": quote()}) == []
        assert ledger.cash == D("100")
    finally:
        ledger.close()


def test_a_transfer_is_never_booked_twice(tmp_path):
    _, ledger, pool, d = build(tmp_path, [transfer_log(ALICE, "25", "0xgift")])
    try:
        d.ingest(time.time(), ETH_USD, {"PONS": quote()})
        ledger.put("donations_block", 50)               # rescan the same blocks
        d.ingest(time.time(), ETH_USD, {"PONS": quote()})
        assert pool.participant(ALICE)["deposited"] == D("25")
    finally:
        ledger.close()


def test_a_tiny_transfer_is_credited_to_the_operator(tmp_path):
    _, ledger, pool, d = build(tmp_path, [transfer_log(ALICE, "0.5", "0xdust")], donation_min_usd="1")
    try:
        booked = d.ingest(time.time(), ETH_USD, {"PONS": quote()})
        assert booked[0]["credited_to"] == OPERATOR
        assert pool.participant(ALICE) is None
        assert ledger.cash == D("100.5")
    finally:
        ledger.close()


def test_a_fresh_run_starts_counting_at_the_head(tmp_path):
    settings = Settings(donations_enabled=True)
    ledger = Ledger(tmp_path / "l.sqlite", settings, "live", D("100"))
    try:
        pool = Pool(ledger)
        d = Donations(settings, ledger, pool, Client([], head=777), Registry(), None, FLY)
        d.start_at_head(0)
        assert ledger.get("donations_block") == 777
    finally:
        ledger.close()


def test_payouts_wait_when_cash_is_needed_for_the_next_order(tmp_path):
    _, ledger, pool, d = build(tmp_path, [])
    try:
        pool.deposit(ALICE, D("100"), D("100"), 1)
        ledger.deposit(D("100"), 1)
        # The pool is up 40%, but almost all of it sits in a position.
        ledger.put("cash", "12")
        ledger.put("positions", {"PONS": str(D("268") / quote().bid)})
        results = d.pay(time.time(), ETH_USD, {"PONS": quote()}, dry_run=True)
        assert results and results[0]["status"] == "DEFERRED_CASH"
    finally:
        ledger.close()


def test_a_dry_run_names_the_payout_without_settling(tmp_path):
    _, ledger, pool, d = build(tmp_path, [])
    try:
        pool.deposit(ALICE, D("100"), D("100"), 1)
        ledger.deposit(D("100"), 1)
        ledger.put("cash", "240")
        results = d.pay(time.time(), ETH_USD, {}, dry_run=True)
        assert results[0]["status"] == "WOULD_SEND" and results[0]["payout"] == "10.00"
        assert pool.participant(ALICE)["paid_out"] == D(0)
    finally:
        ledger.close()


def test_the_fixture_donor_arrives_and_is_paid_when_the_pool_rises(tmp_path):
    settings = Settings(donations_enabled=True, donor_payout_interval_seconds=300)
    ledger = Ledger(tmp_path / "f.sqlite", settings, "paper", D("100"))
    try:
        pool = Pool(ledger, settings.donor_share)
        pool.seed_operator(D("100"), 0)
        d = FixtureDonations(settings, ledger, pool)
        quotes = {"PONS": quote()}
        assert d.ingest(1, ETH_USD, quotes) == []
        booked = d.ingest(2, ETH_USD, quotes)
        assert booked and ledger.cash == D("125")
        ledger.put("cash", "150")                       # the pool made $25
        results = d.pay(3, ETH_USD, quotes)
        assert results and results[0]["status"] == "SIMULATED"
        assert ledger.cash < D("150")                   # the payout left the pool
    finally:
        ledger.close()


def test_payout_conservation_end_to_end(tmp_path):
    """After a settlement plus withdrawal, everyone's value still sums to equity."""
    settings = Settings(donations_enabled=True)
    ledger = Ledger(tmp_path / "c.sqlite", settings, "paper", D("100"))
    try:
        pool = Pool(ledger, "0.5")
        pool.seed_operator(D("100"), 0)
        ledger.deposit(D("100"), 1)
        pool.deposit(ALICE, D("100"), D("100"), 1)
        ledger.put("cash", "240")
        settled = pool.settle(ALICE, D("240"), 2)
        ledger.withdraw(settled["payout"], 2)
        equity = ledger.cash
        total = sum(p["units"] for p in pool.participants()) * pool.nav(equity)
        assert total.quantize(D("0.01")) == equity.quantize(D("0.01"))
        assert (pool.participant(ALICE)["units"] * pool.nav(equity)).quantize(D("0.01")) == D("100")
    finally:
        ledger.close()
