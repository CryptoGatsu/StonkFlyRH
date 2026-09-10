"""Discovery from the v4 PoolManager: hooks, bridges, routes. Fabricated logs."""

import time


from stonkflyrh import v4
from stonkflyrh.chain import checksum
from stonkflyrh.config import D, Settings
from stonkflyrh.discovery import PoolDiscovery
from stonkflyrh.ledger import Ledger
from stonkflyrh.market import RobinhoodChainMarket
from tests.test_discovery import FACTORY, Chain, Registry, addr
from tests.test_safety import QD, WETH as USDG_RAW

# Logs decode to checksummed addresses, so the registry's quote address must be too.
USDG = checksum(USDG_RAW)

ETH_USD = D("2500")
PONS_HOOK = checksum("0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
NOHOOK = v4.NATIVE
PM = checksum("0x" + "9a" * 20)
GOOGL = checksum(addr(700))
COIN = checksum(addr(701))
OTHER_HOOK = checksum("0x" + "77" * 20)


def sorted_pair(a, b):
    return (a, b) if int(a, 16) < int(b, 16) else (b, a)


def init_log(x, y, fee, spacing, hooks, block):
    c0, c1 = sorted_pair(checksum(x), checksum(y))
    key = v4.pool_key(c0, c1, fee, spacing, hooks)
    pad = lambda a: bytes(12) + bytes.fromhex(a[2:])
    signed = lambda v: (v % 2**256).to_bytes(32, "big")
    data = fee.to_bytes(32, "big") + signed(spacing) + pad(hooks) + (2**96).to_bytes(32, "big") + signed(0)
    return {"topics": [v4.initialize_topic(), bytes.fromhex(v4.pool_id(key)[2:]), pad(c0), pad(c1)],
            "data": data, "blockNumber": block, "address": PM}


class V4Chain(Chain):
    """The v3 fake plus a PoolManager log feed and a v4 quoter that prices any route."""

    def logs(self, params, chunk=2000, pause=0, retries=6, max_blocks=None):
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        if max_blocks is not None:
            hi = min(hi, lo + int(max_blocks) - 1)
        want = checksum(params["address"])
        out = [l for l in self.w3.eth.logs if lo <= l["blockNumber"] <= hi and checksum(l.get("address", FACTORY)) == want]
        return out, hi

    def contract(self, address, abi):
        return type("C", (), {"functions": None})()


class Venue:
    """Stands in for V4Venue: quotes every route like a 1%-fee constant-price pool."""

    pool_manager = PM

    def __init__(self, chain):
        self.chain = chain

    def quote_call_for(self, route):
        hops = len(route)

        def call(token_in, token_out, amount_in, fee):
            # Reuse the v3 fake pool model per hop so the screen's probes work.
            out = amount_in
            for _ in range(hops):
                out = self.chain.quote(token_in, token_out, out, fee)
                token_in, token_out = token_out, token_in  # keep direction semantics simple
            return out

        # The fake pool's direction is decided by token_in == USDG; a two-hop
        # route is priced as one hop here, which is enough to exercise routing.
        return lambda token_in, token_out, amount_in, fee: self.chain.quote(token_in, token_out, amount_in, fee)


class V4Registry(Registry):
    quote_address = USDG
    v4 = {"pool_manager": PM, "quoter": PM, "state_view": PM, "universal_router": PM, "permit2": PM,
          "hooks_allow": [PONS_HOOK], "hooks_allow_any": False, "bridges": []}


def build(tmp_path, logs, identities, bridges=None, **overrides):
    from stonkflyrh.safety import RugScreen

    settings = Settings(products=(), discover_v3=False, **overrides)
    ledger = Ledger(tmp_path / "ledger.sqlite", settings, "paper", D("100"))
    chain = V4Chain(logs, 5000, identities)
    registry = V4Registry()
    if bridges:
        registry.v4 = {**registry.v4, "bridges": bridges}
    market = RobinhoodChainMarket.__new__(RobinhoodChainMarket)
    market.s = settings; market.client = chain; market.registry = registry; market.verified = {"pools": {}}
    market.qd = QD; market.products = []; market.history = {}; market.seeded = {}; market.routes = {}
    market.venue = Venue(chain); market.quoter = None
    screen = RugScreen(settings, chain, registry, ledger, chain.quote, market)
    screen._pool_age = lambda entry: 7200.0  # old enough
    disc = PoolDiscovery(settings, chain, registry, ledger, market, screen, FACTORY, market.venue)
    return ledger, registry, market, disc


def test_a_pons_hook_usdg_pool_is_admitted_as_a_v4_product(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, registry, market, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"], report
        entry = ledger.universe()["WOOF"]
        assert entry["venue"] == "v4" and entry["hooks"] == PONS_HOOK and len(entry["route"]) == 1
        assert market.venue_of("WOOF") == "v4" and "WOOF" in market.routes
    finally:
        ledger.close()


def test_an_unknown_hook_is_not_admitted(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, OTHER_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("SUS", 18)})
    try:
        assert disc.scan(time.time(), ETH_USD)["candidates"] == 0
    finally:
        ledger.close()


def test_a_hookless_standard_usdg_pool_becomes_a_bridge_not_a_product(tmp_path):
    logs = [init_log(USDG, GOOGL, 500, 10, NOHOOK, 4800)]
    ledger, _, _, disc = build(tmp_path, logs, {GOOGL: ("GOOGL", 18)})
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert report["candidates"] == 0
        assert GOOGL in disc.bridges()
    finally:
        ledger.close()


def test_a_coin_paired_with_googl_routes_through_the_googl_usdg_pool(tmp_path):
    logs = [init_log(USDG, GOOGL, 500, 10, NOHOOK, 4800), init_log(GOOGL, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, market, disc = build(tmp_path, logs, {GOOGL: ("GOOGL", 18), COIN: ("FLY", 18)})
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["FLY"], report
        entry = ledger.universe()["FLY"]
        assert len(entry["route"]) == 2 and entry["via"] == GOOGL[:8]
        assert entry["route"][0]["fee"] == 500 and entry["route"][1]["hooks"] == PONS_HOOK
    finally:
        ledger.close()


def test_a_registry_bridge_is_used_before_any_pool_is_seen(tmp_path):
    c0, c1 = sorted_pair(USDG, GOOGL)
    bridges = [{"symbol": "GOOGL", "pool": {"currency0": c0, "currency1": c1, "fee": 500, "tickSpacing": 10, "hooks": NOHOOK}}]
    logs = [init_log(GOOGL, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("FLY", 18)}, bridges=bridges)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["FLY"], report
        assert ledger.universe()["FLY"]["via"] == "GOOGL"
    finally:
        ledger.close()


def test_a_pool_with_no_route_waits_for_a_bridge(tmp_path):
    logs = [init_log(GOOGL, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("FLY", 18), GOOGL: ("GOOGL", 18)})
    try:
        assert disc.scan(time.time(), ETH_USD)["candidates"] == 0
        assert len(ledger.get("unrouted_v4")) == 1
        # The bridge appears; the waiting pool is routed on the next scan.
        disc.client.w3.eth.logs.append(init_log(USDG, GOOGL, 500, 10, NOHOOK, 5100))
        disc.client.w3.eth.block_number = 5200
        report = disc.scan(time.time() + 61, ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["FLY"], report
    finally:
        ledger.close()


def test_native_eth_pairs_are_not_routed_yet(tmp_path):
    logs = [init_log(v4.NATIVE, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("ETHY", 18)})
    try:
        assert disc.scan(time.time(), ETH_USD)["candidates"] == 0
        assert ledger.get("unrouted_v4") == []
    finally:
        ledger.close()


def test_v4_screen_infers_depth_and_checks_age(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, registry, market, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    try:
        disc.scan(time.time(), ETH_USD)
        verdict = ledger.screen_raw("WOOF")
        names = {c["name"] for c in verdict["checks"]}
        assert "pool_age" in names and "liquidity" in names and "pool_history" not in names
        assert verdict["approved"]
    finally:
        ledger.close()


def test_a_young_v4_pool_is_withheld(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, registry, market, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    try:
        disc.screen._pool_age = lambda entry: 60.0
        report = disc.scan(time.time(), ETH_USD)
        assert report["added"] == [] and "pool_age" in report["rejected"][0]["reason"]
    finally:
        ledger.close()
