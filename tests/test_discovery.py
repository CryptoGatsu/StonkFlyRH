"""Discovery: new quote-asset pools become candidates, and only screened
survivors become tradable. Factory logs here are in-memory fabrications."""

import time

import pytest

from stonkflyrh.config import D, Settings
from stonkflyrh.discovery import (
    FixtureDiscovery,
    PoolDiscovery,
    clean_symbol,
    decode_pool_created,
    pool_created_topic,
)
from stonkflyrh.ledger import Ledger
from stonkflyrh.market import FixtureMarket
from tests.test_safety import POOL, QD, FakePool, WETH as USDG

ETH_USD = D("2500")
FACTORY = "0x" + "fa" * 20
WETH9 = "0x" + "aa" * 20


def addr(n):
    return "0x" + f"{n:040x}"


def log_for(token, block, fee=10000, pool=None, quote=USDG):
    t0, t1 = sorted([token.lower(), quote.lower()])
    pad = lambda a: bytes(12) + bytes.fromhex(a[2:])
    return {
        "topics": [pool_created_topic(), pad(t0), pad(t1), fee.to_bytes(32, "big")],
        "data": (200).to_bytes(32, "big") + bytes(12) + bytes.fromhex((pool or addr(9000 + block))[2:]),
        "blockNumber": block,
    }


class FakeEth:
    def __init__(self, logs, head):
        self.logs = logs
        self.block_number = head
        self.queries = []

    def get_logs(self, q):
        self.queries.append(q)
        return [l for l in self.logs if q["fromBlock"] <= l["blockNumber"] <= q["toBlock"]]


class Chain(FakePool):
    """The screen's pool model plus a factory log feed and token identities."""

    def __init__(self, logs, head, identities, **pool):
        super().__init__(**pool)
        self.w3 = type("W3", (), {})()
        self.w3.eth = FakeEth(logs, head)
        self.identities = identities

    def token_identity(self, address):
        if address not in self.identities:
            raise RuntimeError("no such token")
        symbol, decimals = self.identities[address]
        return {"address": address, "symbol": symbol, "decimals": decimals}

    def logs(self, params, chunk=2000, pause=0, retries=6, max_blocks=None):
        # Mirrors ChainClient.logs without the sleeps.
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        if max_blocks is not None:
            hi = min(hi, lo + int(max_blocks) - 1)
        out = []
        for start in range(lo, hi + 1, chunk):
            end = min(hi, start + chunk - 1)
            out += self.w3.eth.get_logs({**params, "fromBlock": start, "toBlock": end})
        return out, hi


class Registry:
    quote_address = USDG
    quote_decimals = QD
    weth = WETH9
    quote_symbol = "USDG"

    def __init__(self):
        self.tokens = {}

    def add_token(self, entry):
        self.tokens[entry["symbol"]] = dict(entry)

    def remove_token(self, symbol):
        self.tokens.pop(symbol, None)

    def token(self, symbol):
        return self.tokens[symbol]

    def pool_fee(self, symbol, default):
        return self.tokens.get(symbol, {}).get("pool_fee", default)


def build(tmp_path, chain, **overrides):
    from stonkflyrh.safety import RugScreen

    settings = Settings(products=("PONS",), **overrides)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper", D("100"))
    registry = Registry()
    registry.add_token({"symbol": "PONS", "address": addr(1), "decimals": 18, "pool_fee": 10000})
    market = FixtureMarket(settings)
    screen = RugScreen(settings, chain, registry, ledger, chain.quote)
    return settings, ledger, registry, market, PoolDiscovery(
        settings, chain, registry, ledger, market, screen, FACTORY
    )


# -- decoding --------------------------------------------------------------


def test_the_topic_is_the_uniswap_v3_pool_created_signature():
    assert pool_created_topic() == "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"


def test_a_factory_log_decodes_to_its_pool():
    d = decode_pool_created(log_for(addr(42), 500, fee=3000, pool=addr(77)))
    assert d["fee"] == 3000 and d["block"] == 500 and d["pool"].lower() == addr(77)
    assert {d["token0"].lower(), d["token1"].lower()} == {addr(42).lower(), USDG.lower()}


def test_a_log_of_the_wrong_shape_is_refused():
    bad = log_for(addr(42), 500)
    bad["topics"] = bad["topics"][:2]
    with pytest.raises(ValueError):
        decode_pool_created(bad)


# -- symbols ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("pepe", "PEPE"), ("$PONS 🚀", "PONS"), ("a", "AX"), ("", "TOKEN"), ("x" * 40, "X" * 10)],
)
def test_symbols_are_normalised(raw, expected):
    assert clean_symbol(raw, addr(1), set()) == expected


def test_a_colliding_symbol_gets_an_address_suffix():
    key = clean_symbol("PEPE", "0xAbCd" + "0" * 36, {"PEPE"})
    assert key == "PEPE_ABCD"


# -- scanning --------------------------------------------------------------


def test_a_clean_new_pool_joins_the_universe(tmp_path):
    chain = Chain([log_for(addr(50), 1000)], 1200, {addr(50): ("WOOF", 18)})
    _, ledger, registry, market, disc = build(tmp_path, chain)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert report["candidates"] == 1
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
        assert "WOOF" in ledger.universe() and "WOOF" in market.products
        assert ledger.universe()["WOOF"]["source"] == "factory:PoolCreated"
        assert ledger.get("discovery_block") == 1200
    finally:
        ledger.close()


def test_a_pool_that_fails_the_screen_is_rejected_and_remembered(tmp_path):
    chain = Chain([log_for(addr(51), 1000)], 1200, {addr(51): ("SCAM", 18)}, sellable=False)
    _, ledger, registry, market, disc = build(tmp_path, chain)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert report["added"] == []
        assert "could not be sold back" in report["rejected"][0]["reason"]
        assert "SCAM" not in ledger.universe() and "SCAM" not in registry.tokens
        # Rescanning the same blocks does not screen the same pool again.
        ledger.put("discovery_block", 900)
        report = disc.scan(time.time() + 700, ETH_USD)
        assert report["skipped"] == 1 and report["candidates"] == 0
    finally:
        ledger.close()


def test_only_quote_asset_pools_are_candidates(tmp_path):
    other = addr(60)
    logs = [log_for(addr(52), 1000, quote=other), log_for(addr(53), 1001, quote=WETH9)]
    chain = Chain(logs, 1200, {addr(52): ("A1", 18), addr(53): ("B2", 18)})
    _, ledger, _, _, disc = build(tmp_path, chain)
    try:
        assert disc.scan(time.time(), ETH_USD)["candidates"] == 0
    finally:
        ledger.close()


def test_unsupported_fee_tiers_are_ignored(tmp_path):
    chain = Chain([log_for(addr(54), 1000, fee=100)], 1200, {addr(54): ("TINY", 18)})
    _, ledger, _, _, disc = build(tmp_path, chain)
    try:
        assert disc.scan(time.time(), ETH_USD)["candidates"] == 0
    finally:
        ledger.close()


def test_the_universe_cap_holds(tmp_path):
    logs = [log_for(addr(70 + i), 1000 + i) for i in range(4)]
    ids = {addr(70 + i): (f"C{i}", 18) for i in range(4)}
    chain = Chain(logs, 1200, ids)
    _, ledger, _, _, disc = build(tmp_path, chain, max_products=3)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert len(report["added"]) == 2  # PONS is the seed; two more fit
        assert any(r["reason"] == "universe full" for r in report["rejected"])
        assert len(ledger.universe()) == 3
    finally:
        ledger.close()


def test_scanning_is_chunked_and_resumes_from_the_last_block(tmp_path):
    chain = Chain([], 20000, {})
    _, ledger, _, _, disc = build(tmp_path, chain, discovery_lookback_blocks=10000)
    try:
        disc.scan(time.time(), ETH_USD)
        spans = [(q["fromBlock"], q["toBlock"]) for q in chain.w3.eth.queries]
        assert spans[0][0] == 10001 and spans[-1][1] == 20000
        assert all(hi - lo + 1 <= PoolDiscovery.CHUNK for lo, hi in spans)
        chain.w3.eth.queries.clear()
        chain.w3.eth.block_number = 20500
        disc.scan(time.time() + 700, ETH_USD)
        assert chain.w3.eth.queries[0]["fromBlock"] == 20001
    finally:
        ledger.close()


def test_a_blocklisted_symbol_never_comes_back(tmp_path):
    chain = Chain([log_for(addr(55), 1000)], 1200, {addr(55): ("RUGME", 18)})
    _, ledger, _, _, disc = build(tmp_path, chain)
    try:
        ledger.block("RUGME", "rugged earlier", time.time())
        report = disc.scan(time.time(), ETH_USD)
        assert report["rejected"][0]["reason"] == "blocklisted"
    finally:
        ledger.close()


def test_an_unreadable_token_is_rejected_not_fatal(tmp_path):
    chain = Chain([log_for(addr(56), 1000)], 1200, {})
    _, ledger, _, _, disc = build(tmp_path, chain)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert report["rejected"][0]["reason"] == "unreadable token"
    finally:
        ledger.close()


def test_scans_respect_the_interval(tmp_path):
    chain = Chain([], 100, {})
    _, ledger, _, _, disc = build(tmp_path, chain)
    try:
        now = time.time()
        assert disc.due(now)
        disc.scan(now, ETH_USD)
        assert not disc.due(now + 10)
        assert disc.due(now + 601)
    finally:
        ledger.close()


def test_a_long_backlog_is_spread_over_several_scans(tmp_path):
    chain = Chain([], 500000, {})
    _, ledger, _, _, disc = build(tmp_path, chain, discovery_lookback_blocks=400000)
    try:
        now = time.time()
        first = disc.scan(now, ETH_USD)
        assert first["to_block"] == 100000 + PoolDiscovery.MAX_BLOCKS_PER_SCAN
        assert first["backlog_blocks"] > 0
        # Behind, so the next scan is due in a minute rather than ten.
        assert disc.due(now + 61)
        second = disc.scan(now + 61, ETH_USD)
        assert second["from_block"] == first["to_block"]
        assert second["backlog_blocks"] < first["backlog_blocks"]
    finally:
        ledger.close()


# -- pruning ---------------------------------------------------------------


def test_an_unheld_discovered_token_that_stops_clearing_is_dropped(tmp_path):
    chain = Chain([log_for(addr(57), 1000)], 1200, {addr(57): ("FADE", 18)})
    settings, ledger, registry, market, disc = build(tmp_path, chain, screen_ttl_seconds=60)
    try:
        disc.scan(time.time(), ETH_USD)
        assert "FADE" in ledger.universe()
        chain.sellable = False
        dropped = disc.prune(time.time() + 120, ETH_USD)
        assert [d["symbol"] for d in dropped] == ["FADE"]
        assert "FADE" not in ledger.universe() and "FADE" not in market.products
        assert "PONS" in ledger.universe()  # seeds are never pruned
    finally:
        ledger.close()


def test_a_held_token_is_never_pruned(tmp_path):
    chain = Chain([log_for(addr(58), 1000)], 1200, {addr(58): ("HELD", 18)})
    _, ledger, _, _, disc = build(tmp_path, chain, screen_ttl_seconds=60)
    try:
        disc.scan(time.time(), ETH_USD)
        ledger.put("positions", {"HELD": "1000"})
        chain.sellable = False
        assert disc.prune(time.time() + 120, ETH_USD) == []
        assert "HELD" in ledger.universe()
    finally:
        ledger.close()


# -- the universe in the guard ---------------------------------------------


def test_the_guard_trades_the_universe_not_the_seed_list(tmp_path):
    from stonkflyrh.risk import Guard, Veto
    from tests.test_execution import quote

    settings = Settings(products=("PONS",))
    ledger = Ledger(tmp_path / "g.sqlite", settings, "paper", D("100"))
    try:
        guard = Guard(settings, ledger, tmp_path / "STOP")
        with pytest.raises(Veto):
            guard.plan("WOOF", "BUY", {"PONS": quote(), "WOOF": quote(product="WOOF")}, ETH_USD)
        ledger.add_to_universe({"symbol": "WOOF", "address": addr(1), "decimals": 18, "pool_fee": 10000, "pool": POOL, "source": "factory:PoolCreated", "added_at": 0})
        plan = guard.plan("WOOF", "BUY", {"PONS": quote(), "WOOF": quote(product="WOOF")}, ETH_USD)
        assert plan["product"] == "WOOF"
    finally:
        ledger.close()


def test_the_fixture_discovery_adds_one_token_on_its_third_scan(tmp_path):
    settings = Settings()
    ledger = Ledger(tmp_path / "f.sqlite", settings, "paper", D("100"))
    try:
        market = FixtureMarket(settings)
        disc = FixtureDiscovery(settings, ledger, market)
        for _ in range(3):
            disc.scan(time.time(), ETH_USD)
        assert "NEWCOIN" in ledger.universe() and "NEWCOIN" in market.products
        assert market.snapshot(D("10"))["NEWCOIN"].bid > 0
    finally:
        ledger.close()


def test_seed_lists_are_not_part_of_the_protocol_signature():
    assert Settings(products=("PONS",)).signature() == Settings(products=("PONS", "LONG")).signature()


# -- a scan that dies half-way keeps what it reached -----------------------


class FlakyChain(Chain):
    """Rate-limits once, on the second log request, like a public RPC."""

    def __init__(self, *a, fail_on=2, **kw):
        super().__init__(*a, **kw)
        self.calls = 0
        self.fail_on = fail_on

    def logs(self, params, **kw):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("429 Too Many Requests")
        return super().logs(params, **kw)


def test_progress_survives_an_rpc_error_mid_scan(tmp_path):
    chain = FlakyChain([log_for(addr(60), 19500)], 20000, {addr(60): ("LATE", 18)},
                       fail_on=3)
    _, ledger, _, market, disc = build(tmp_path, chain, discovery_lookback_blocks=18000)
    try:
        with pytest.raises(RuntimeError, match="429"):
            disc.scan(time.time(), ETH_USD)
        # Two windows of 6000 blocks were fetched before the error; the block
        # reached is on record and nothing before it is fetched again.
        reached = ledger.get("discovery_block")
        assert reached == 2000 + 2 * PoolDiscovery.WINDOW
        chain.w3.eth.queries.clear()
        report = disc.scan(time.time() + 61, ETH_USD)
        assert chain.w3.eth.queries[0]["fromBlock"] == reached + 1
        assert [a["symbol"] for a in report["added"]] == ["LATE"]
        assert ledger.get("discovery_block") == 20000
    finally:
        ledger.close()


def test_a_candidate_whose_screen_failed_stays_queued(tmp_path):
    chain = Chain([log_for(addr(64), 1000), log_for(addr(65), 1100)], 1200,
                  {addr(64): ("ONE", 18), addr(65): ("TWO", 18)})
    _, ledger, _, market, disc = build(tmp_path, chain)
    try:
        real = disc.screen.assess
        state = {"fail": True}

        def flaky(*a, **kw):
            if state["fail"]:
                state["fail"] = False
                raise ConnectionError("read timed out")
            return real(*a, **kw)

        disc.screen.assess = flaky
        with pytest.raises(ConnectionError):
            disc.scan(time.time(), ETH_USD)
        pending = ledger.get("pending_candidates")
        assert [c["token"].lower() for c in pending] == [addr(65).lower(), addr(64).lower()]
        assert disc.due(time.time() + 61)                              # queued work is due soon
        report = disc.scan(time.time() + 61, ETH_USD)
        assert sorted(a["symbol"] for a in report["added"]) == ["ONE", "TWO"]
        assert ledger.get("pending_candidates") == []
    finally:
        ledger.close()
