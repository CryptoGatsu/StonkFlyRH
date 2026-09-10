"""Airdropping the coin: who qualifies, what is never a recipient, and the
budget that bounds a round. Sends go through the same recoverable transfer path
as donor payouts; here the chain is faked and the transfer is observed."""

import time

import pytest

from stonkflyrh.airdrop import Airdrop
from stonkflyrh.chain import checksum
from stonkflyrh.config import D, Settings, to_wei
from stonkflyrh.donations import transfer_topic
from stonkflyrh.ledger import Ledger

COIN = checksum("0x" + "c0" * 20)
DEPLOYER = checksum("0x" + "d0" * 20)
FLY = checksum("0x" + "33" * 20)
ROUTER = checksum("0x" + "40" * 20)
POOL_MANAGER = checksum("0x" + "41" * 20)
UROUTER = checksum("0x" + "42" * 20)
POOL_A = checksum("0x" + "50" * 20)
TOKEN_A = checksum("0x" + "0a" * 20)
TOKEN_B = checksum("0x" + "0b" * 20)
ALICE = checksum("0x" + "a1" * 20)   # bought A and B, still holds both
BOB = checksum("0x" + "b2" * 20)     # bought A, sold it all
CAROL = checksum("0x" + "c3" * 20)   # bought A, holds it
DAVE = checksum("0x" + "d4" * 20)    # a contract that bought A and B
ERIN = checksum("0x" + "e5" * 20)    # bought A late, holds it


def pad(addr):
    return bytes(12) + bytes.fromhex(addr[2:])


def buy(token, source, buyer, amount, block, tx="0x" + "77" * 32):
    return {
        "address": token,
        "topics": [transfer_topic(), pad(source), pad(buyer)],
        "data": (amount).to_bytes(32, "big"),
        "blockNumber": block,
        "transactionHash": tx,
        "logIndex": 0,
    }


class Erc20:
    """functions.balanceOf(addr).call() against a dict, transfer() records intent."""

    def __init__(self, balances):
        self._balances = balances
        self.functions = self
        self.transfers = []

    def balanceOf(self, address):
        bal = self._balances.get(checksum(address), 0)
        return type("Call", (), {"call": staticmethod(lambda: bal)})()

    def transfer(self, to, amount):
        self.transfers.append((checksum(to), int(amount)))
        return type(
            "Tx", (), {"build_transaction": staticmethod(lambda p: {"to": to, "value": 0, **p})}
        )()


class Eth:
    def __init__(self, logs, head):
        self.logs = logs
        self.block_number = head
        self.receipts = {}
        self.sent = []

    def get_logs(self, q):
        want = checksum(q["address"])
        froms = {t.lower() for t in q["topics"][1]}
        return [
            l for l in self.logs
            if q["fromBlock"] <= l["blockNumber"] <= q["toBlock"]
            and checksum(l["address"]) == want
            and ("0x" + l["topics"][1].hex()).lower() in froms
        ]

    def send_raw_transaction(self, raw):
        self.sent.append(raw)

    def get_transaction_receipt(self, tx_hash):
        return self.receipts.get(tx_hash, {"status": 1})


class Client:
    net = type("Net", (), {"chain_id": 4663, "tx_url": staticmethod(lambda h: "https://x/" + h)})()

    def __init__(self, logs, head, balances, contracts=(), eth_balance=10**18):
        self.w3 = type("W3", (), {})()
        self.w3.eth = Eth(logs, head)
        self.balances = balances          # token -> {holder: amount}
        self.contracts = set(contracts)
        self.eth_balance = eth_balance
        self.tokens = {}

    def erc20(self, address):
        address = checksum(address)
        if address not in self.tokens:
            self.tokens[address] = Erc20(self.balances.get(address, {}))
        return self.tokens[address]

    def token_identity(self, address):
        return {"address": address, "symbol": "COIN", "decimals": 18}

    def has_code(self, address):
        return checksum(address) in self.contracts

    def balance(self, address):
        return self.eth_balance

    def gas_price(self):
        return 10**8

    def nonce(self, address):
        return 7

    def logs(self, params, chunk=2000, pause=0, retries=6, max_blocks=None):
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        return self.w3.eth.get_logs({**params, "fromBlock": lo, "toBlock": hi}), hi


class Registry:
    router = ROUTER
    quote_address = checksum("0x" + "11" * 20)
    weth = checksum("0x" + "22" * 20)
    v4 = {"pool_manager": POOL_MANAGER, "universal_router": UROUTER}


class Account:
    address = DEPLOYER

    def sign_transaction(self, tx):
        return type("Signed", (), {"hash": bytes.fromhex("99" * 32), "raw_transaction": b"raw"})()


UNIVERSE = {
    "AAA": {"symbol": "AAA", "address": TOKEN_A, "pool": POOL_A, "venue": "v3"},
    "BBB": {"symbol": "BBB", "address": TOKEN_B, "pool": "0xpool", "venue": "v4"},
}

LOGS = [
    buy(TOKEN_A, POOL_A, ALICE, 5, 1000),
    buy(TOKEN_B, POOL_MANAGER, ALICE, 5, 1500),
    buy(TOKEN_A, POOL_A, BOB, 5, 1100),
    buy(TOKEN_A, ROUTER, CAROL, 5, 1200),
    buy(TOKEN_A, POOL_A, DAVE, 5, 900),
    buy(TOKEN_B, UROUTER, DAVE, 5, 950),
    buy(TOKEN_A, POOL_A, ERIN, 5, 1900),
    buy(TOKEN_A, POOL_A, FLY, 5, 1300),            # the fly's own buy
    buy(TOKEN_A, ALICE, CAROL, 5, 1400),           # a wallet-to-wallet transfer is not a buy
    buy(TOKEN_A, POOL_A, ALICE, 0, 1600),          # a zero transfer
]

BALANCES = {
    TOKEN_A: {ALICE: 5, CAROL: 5, DAVE: 5, ERIN: 5, BOB: 0},
    TOKEN_B: {ALICE: 5, DAVE: 5},
    COIN: {DEPLOYER: 10**18 * 10_000},
}


def build(tmp_path, account=None, head=2000, balances=None, **overrides):
    settings = Settings(coin_address=COIN, airdrop_enabled=True, airdrop_lookback_blocks=1500,
                        **overrides)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "live" if account else "paper", D("100"))
    client = Client(LOGS, head, balances or BALANCES, contracts={DAVE, POOL_A})
    airdrop = Airdrop(settings, client, Registry(), ledger, account, COIN, DEPLOYER, excluded=[FLY])
    return settings, ledger, client, airdrop


def test_census_records_buyers_and_ignores_the_rest(tmp_path):
    _, ledger, client, airdrop = build(tmp_path)
    try:
        report = airdrop.census(time.time(), UNIVERSE)
        rows = {(r[0], r[2]) for r in ledger.db.execute("SELECT address,token,symbol FROM holders")}
        assert (ALICE, "AAA") in rows and (ALICE, "BBB") in rows
        assert (CAROL, "AAA") in rows and (ERIN, "AAA") in rows
        assert (FLY, "AAA") not in rows                    # excluded wallet
        assert report["buys"] == 7 and ledger.get("airdrop_block") == 2000
        # A second census picks up from where the first stopped.
        client.w3.eth.block_number = 2500
        again = airdrop.census(time.time(), UNIVERSE)
        assert again["from_block"] == 2000 and again["buys"] == 0
    finally:
        ledger.close()


def test_candidates_rank_holders_of_more_screened_tokens_first(tmp_path):
    _, ledger, _, airdrop = build(tmp_path)
    try:
        airdrop.census(time.time(), UNIVERSE)
        picks = airdrop.candidates(time.time(), 10)
        assert [p["address"] for p in picks] == [ALICE, CAROL, ERIN]
        assert picks[0]["bought"] == 2 and picks[0]["holds"] == ["AAA", "BBB"]
        # Bob sold everything, Dave is a contract: neither qualifies.
        assert BOB not in {p["address"] for p in picks}
        assert DAVE not in {p["address"] for p in picks}
        assert "still holds AAA" in picks[1]["reason"]
    finally:
        ledger.close()


def test_min_tokens_raises_the_bar(tmp_path):
    _, ledger, _, airdrop = build(tmp_path, airdrop_min_tokens=2)
    try:
        airdrop.census(time.time(), UNIVERSE)
        assert [p["address"] for p in airdrop.candidates(time.time(), 10)] == [ALICE]
    finally:
        ledger.close()


def test_a_paper_round_is_a_dry_run_and_sends_nothing(tmp_path):
    _, ledger, client, airdrop = build(tmp_path, airdrop_recipients_per_round=2)
    try:
        report = airdrop.round(time.time(), UNIVERSE)
        assert report["dry_run"] is True and report["sent"] == []
        assert [w["address"] for w in report["would_send"]] == [ALICE, CAROL]
        assert report["would_send"][0]["amount"] == "1000"
        assert client.w3.eth.sent == []
        assert ledger.events("airdrops")[0]["dry_run"] is True
        assert airdrop.report(time.time())["wallets_seen"] == 5
    finally:
        ledger.close()


def test_a_live_round_sends_from_the_deployer_and_records_each_drop(tmp_path):
    _, ledger, client, airdrop = build(tmp_path, account=Account(), airdrop_recipients_per_round=2)
    try:
        now = time.time()
        report = airdrop.round(now, UNIVERSE)
        assert report["dry_run"] is False
        assert [w["address"] for w in report["sent"]] == [ALICE, CAROL]
        assert all(w["status"] == "SENT" for w in report["sent"])
        coin = client.erc20(COIN)
        assert coin.transfers == [(ALICE, to_wei("1000", 18)), (CAROL, to_wei("1000", 18))]
        history = airdrop.history()
        assert {h["address"] for h in history} == {ALICE, CAROL}
        assert all(h["status"] == "SENT" and h["tx_hash"] for h in history)
        # The next round moves on to wallets not yet dropped.
        second = airdrop.round(now + 3600, UNIVERSE)
        assert [w["address"] for w in second["sent"]] == [ERIN]
        assert airdrop.report(now + 3600)["drops_sent"] == 3
    finally:
        ledger.close()


def test_the_budget_bounds_a_round(tmp_path):
    balances = {**BALANCES, COIN: {DEPLOYER: to_wei("1500", 18)}}
    _, ledger, _, airdrop = build(tmp_path, account=Account(), balances=balances,
                                  airdrop_recipients_per_round=5)
    try:
        budget = airdrop.budget(time.time())
        assert budget["sendable"] == 1                     # 1500 coins, 1000 a drop
        report = airdrop.round(time.time(), UNIVERSE)
        assert [w["address"] for w in report["sent"]] == [ALICE]
        balances[COIN][DEPLOYER] = to_wei("500", 18)       # the fake chain does not debit
        empty = airdrop.round(time.time() + 3600, UNIVERSE)
        assert empty["sent"] == [] and "less than one drop" in empty["skipped"]
    finally:
        ledger.close()


def test_the_reserve_and_daily_cap_hold(tmp_path):
    _, ledger, _, airdrop = build(tmp_path, account=Account(), airdrop_reserve="9500",
                                  airdrop_daily_cap="1000")
    try:
        assert airdrop.budget(time.time())["sendable"] == 0
        airdrop.s = Settings(coin_address=COIN, airdrop_enabled=True, airdrop_daily_cap="1000")
        assert airdrop.budget(time.time())["sendable"] == 1
        airdrop.round(time.time(), UNIVERSE)
        budget = airdrop.budget(time.time())
        assert budget["sendable"] == 0 and budget["why_not_more"] == "daily cap reached"
    finally:
        ledger.close()


def test_no_gas_means_no_round(tmp_path):
    _, ledger, client, airdrop = build(tmp_path, account=Account())
    client.eth_balance = 0
    try:
        report = airdrop.round(time.time(), UNIVERSE)
        assert report["sent"] == [] and "no ETH" in report["skipped"]
    finally:
        ledger.close()


def test_an_unresolved_send_blocks_the_next_round_until_settled(tmp_path):
    _, ledger, client, airdrop = build(tmp_path, account=Account())
    try:
        ledger.db.execute(
            "INSERT INTO airdrops(created,address,amount_wei,reason,status,tx_hash) VALUES (?,?,?,?,?,?)",
            (time.time(), ALICE, "1", "test", "UNKNOWN", "0xdead"),
        )
        client.w3.eth.receipts["0xdead"] = None
        with pytest.raises(RuntimeError, match="no receipt"):
            airdrop.round(time.time(), UNIVERSE)
        client.w3.eth.receipts["0xdead"] = {"status": 0}
        report = airdrop.round(time.time(), UNIVERSE)
        # The reverted drop is REJECTED; Alice is eligible again and is dropped.
        assert ALICE in {w["address"] for w in report["sent"]}
        statuses = [r[0] for r in ledger.db.execute("SELECT status FROM airdrops WHERE address=?", (ALICE,))]
        assert sorted(statuses) == ["REJECTED", "SENT"]
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"airdrop_enabled": True},                          # no coin
        {"airdrop_amount": "0"},
        {"airdrop_recipients_per_round": 0},
        {"airdrop_interval_seconds": 60},
        {"airdrop_daily_cap": "10", "airdrop_amount": "100"},
        {"airdrop_min_tokens": 0},
    ],
)
def test_airdrop_settings_bounds(changes):
    with pytest.raises(ValueError):
        Settings(**changes)
