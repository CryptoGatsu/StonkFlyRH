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


def test_an_unfilled_registry_bridge_is_ignored_not_fatal(tmp_path):
    zero = v4.NATIVE
    bridges = [{"symbol": "GOOGL", "pool": {"currency0": zero, "currency1": zero, "fee": 500, "tickSpacing": 10, "hooks": zero}},
               {"symbol": "BAD", "pool": {"currency0": checksum(addr(1)), "currency1": checksum(addr(2)), "fee": 500, "tickSpacing": 10, "hooks": zero}}]
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)}, bridges=bridges)
    try:
        assert disc.bridges() == {}
        assert [a["symbol"] for a in disc.scan(time.time(), ETH_USD)["added"]] == ["WOOF"]
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


def test_an_eth_paired_launch_routes_through_the_usdg_eth_pool(tmp_path):
    logs = [init_log(v4.NATIVE, USDG, 500, 10, NOHOOK, 4800), init_log(v4.NATIVE, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("ETHY", 18)})
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["ETHY"], report
        entry = ledger.universe()["ETHY"]
        assert entry["via"] == "ETH" and len(entry["route"]) == 2
        assert entry["route"][0]["currency0"] == v4.NATIVE
    finally:
        ledger.close()


def test_an_eth_paired_launch_waits_without_a_usdg_eth_pool(tmp_path):
    logs = [init_log(v4.NATIVE, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("ETHY", 18)})
    try:
        assert disc.scan(time.time(), ETH_USD)["candidates"] == 0
        assert len(ledger.get("unrouted_v4")) == 1
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


# -- pool state: empty pools wait, depth comes from liquidity, bridges are probed


class StateVenue(Venue):
    """A venue that also answers StateView questions. `pools` maps a pool id to
    (liquidity, sqrtPriceX96); anything else has no liquidity."""

    def __init__(self, chain, pools):
        super().__init__(chain)
        self.pools = pools
        self.probed = []

    def liquidity(self, key):
        self.probed.append(key)
        return self.pools.get(v4.pool_id(key), (0, 0))[0]

    def slot0(self, key):
        liquidity, sqrt_p = self.pools.get(v4.pool_id(key), (0, 0))
        return {"sqrtPriceX96": sqrt_p, "tick": 0, "protocolFee": 0, "lpFee": 30000}

    def quote_path(self, keys, currency_in, amount_in):
        return amount_in  # one unit of any bridge asset is worth one USDG here


def build_state(tmp_path, logs, identities, pools, **overrides):
    ledger, registry, market, disc = build(tmp_path, logs, identities, **overrides)
    venue = StateVenue(disc.client, pools)
    market.venue = venue
    disc.venue = venue
    return ledger, registry, market, disc, venue


def coin_key():
    c0, c1 = sorted_pair(USDG, COIN)
    return v4.pool_key(c0, c1, 30000, 60, PONS_HOOK)


def test_an_empty_pool_is_looked_at_again_not_rejected(tmp_path):
    """Pons initialises the pool at token creation and fills it at graduation."""
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, registry, market, disc, venue = build_state(tmp_path, logs, {COIN: ("WOOF", 18)}, {})
    try:
        now = time.time()
        report = disc.scan(now, ETH_USD)
        assert report["added"] == [] and report["rejected"] == []
        assert [r["symbol"] for r in report["retried"]] == ["WOOF"]
        assert "no liquidity yet" in report["retried"][0]["reason"]
        assert COIN not in ledger.seen_candidates()             # not remembered as rejected
        pending = ledger.get("pending_candidates")
        assert len(pending) == 1 and pending[0]["not_before"] > now
        # Too soon: it waits. Then the pool fills and it is admitted.
        assert disc.scan(now + 60, ETH_USD)["waiting_for_liquidity"] == 1
        venue.pools[v4.pool_id(coin_key())] = (10**20, 2**96)
        report = disc.scan(now + 2000, ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
        assert ledger.get("pending_candidates") == []
    finally:
        ledger.close()


def test_a_pool_that_never_fills_is_given_up_on(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc, _ = build_state(tmp_path, logs, {COIN: ("WOOF", 18)}, {})
    try:
        now = time.time()
        for i in range(PoolDiscovery.MAX_EMPTY_POOL_RETRIES + 1):
            report = disc.scan(now + i * 2000, ETH_USD)
        assert report["rejected"][0]["reason"] == "pool never received liquidity"
        assert COIN in ledger.seen_candidates()
    finally:
        ledger.close()


def test_v4_depth_is_read_from_pool_liquidity(tmp_path):
    """A pool with USDG as currency1 at price 1: reserve1 = L * sqrtP / 2^96."""
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    # 3000 USDG (6 decimals) of virtual reserve on the quote side -> ~$6000 depth
    pools = {v4.pool_id(coin_key()): (3000 * 10**QD, 2**96)}
    ledger, _, _, disc, _ = build_state(tmp_path, logs, {COIN: ("WOOF", 18)}, pools,
                                        min_liquidity_usd="7000")
    try:
        report = disc.scan(time.time(), ETH_USD)
        reason = report["rejected"][0]["reason"]
        assert "liquidity" in reason and "from pool liquidity" in reason
        assert "$6000" in reason
        # Lower the floor below the read depth and the same pool clears.
        disc.s = disc.screen.s = Settings(products=(), discover_v3=False, min_liquidity_usd="5000")
        ledger.put("discovery_block", 4000)
        ledger.db.execute("DELETE FROM candidates")
        report = disc.scan(time.time() + 700, ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
    finally:
        ledger.close()


def test_the_registry_keeps_the_block_a_pool_was_created_in(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, registry, _, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    try:
        disc.scan(time.time(), ETH_USD)
        assert registry.token("WOOF")["discovered_block"] == 4900
        assert registry.token("WOOF")["hooks"] == PONS_HOOK
    finally:
        ledger.close()


def test_the_eth_bridge_is_probed_from_pool_state_on_start(tmp_path):
    """No USDG/ETH Initialize log in range, but the pool exists: found anyway."""
    c0, c1 = sorted_pair(v4.NATIVE, USDG)
    eth_pool = v4.pool_key(c0, c1, 500, 10, NOHOOK)
    shallow = v4.pool_key(c0, c1, 3000, 60, NOHOOK)
    pools = {v4.pool_id(eth_pool): (10**24, 2**96), v4.pool_id(shallow): (10**20, 2**96)}
    logs = [init_log(v4.NATIVE, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc, venue = build_state(tmp_path, logs, {COIN: ("WOOF", 18)}, pools)
    try:
        disc._seed_bridges()
        bridge = disc.bridges()[v4.NATIVE]
        assert bridge["symbol"] == "ETH" and bridge["source"] == "probed"
        assert bridge["route"][0]["fee"] == 500                 # the deeper of the two
        venue.pools[v4.pool_id(v4.pool_key(*sorted_pair(v4.NATIVE, COIN), 30000, 60, PONS_HOOK))] = (10**22, 2**96)
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
        assert len(ledger.universe()["WOOF"]["route"]) == 2 and ledger.universe()["WOOF"]["via"] == "ETH"
    finally:
        ledger.close()


def test_an_unknown_pair_asset_is_probed_for_a_bridge_once_in_a_while(tmp_path):
    c0, c1 = sorted_pair(GOOGL, USDG)
    googl_pool = v4.pool_key(c0, c1, 500, 10, NOHOOK)
    logs = [init_log(GOOGL, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc, venue = build_state(tmp_path, logs, {COIN: ("WOOF", 18)}, {})
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert report["added"] == [] and len(ledger.get("unrouted_v4")) == 1
        assert GOOGL in ledger.get("bridge_probes")                 # asked, nothing there
        asked = len(venue.probed)
        ledger.put("discovery_block", 4000)
        disc.scan(time.time() + 61, ETH_USD)
        assert len(venue.probed) == asked                           # not asked again so soon
        # The pool appears and the probe window passes: routed through GOOGL.
        venue.pools[v4.pool_id(googl_pool)] = (10**24, 2**96)
        venue.pools[v4.pool_id(v4.pool_key(*sorted_pair(GOOGL, COIN), 30000, 60, PONS_HOOK))] = (10**22, 2**96)
        ledger.put("bridge_probes", {})
        ledger.put("discovery_block", 4000)
        report = disc.scan(time.time() + 122, ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
        assert ledger.universe()["WOOF"]["via"] == GOOGL[:8]
    finally:
        ledger.close()


def test_rejections_from_the_broken_age_check_are_forgotten_and_rescanned(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    ledger, _, _, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    try:
        now = time.time()
        ledger.mark_candidate(COIN, "pool_age: pool creation block unknown; liquidity: ...", now)
        ledger.mark_candidate(GOOGL, "ownership: owner is still 0xabc", now)
        ledger.put("discovery_block", 5000)
        report = disc.heal(now)
        assert report["cleared_stale_rejections"] == 1
        assert ledger.seen_candidates() == [GOOGL]              # a real judgement stays
        assert ledger.get("discovery_block") == 5000 - Settings().discovery_lookback_blocks or ledger.get("discovery_block") == 0
        assert disc.heal(now + 1) is None                        # once only
        # The rewound scan sees the pool again and, with the fixed check, admits it.
        scan = disc.scan(now + 2, ETH_USD)
        assert [a["symbol"] for a in scan["added"]] == ["WOOF"]
    finally:
        ledger.close()


# -- the buy itself must simulate on a live run ------------------------------


class ExecVenue(StateVenue):
    """A venue whose router refuses buys of one token with a bare revert."""

    def __init__(self, chain, pools, refuse=()):
        super().__init__(chain, pools)
        self.refuse = {checksum(a) for a in refuse}
        self.simulated = []

    def swap_call(self, route, token_in, amount_in, min_out, deadline):
        key = route[-1]
        token = key["currency1"] if checksum(key["currency0"]) == USDG else key["currency0"]
        venue = self

        class Call:
            address = PM

            def call(self, tx):
                venue.simulated.append((token, tx["from"]))
                if checksum(token) in venue.refuse:
                    from web3.exceptions import ContractLogicError

                    raise ContractLogicError("execution reverted")

            def _encode_transaction_data(self):
                return "0x"

        return Call()


def test_a_token_whose_buy_reverts_is_withheld_on_a_live_run(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    pools = {v4.pool_id(coin_key()): (10**20, 2**96)}
    ledger, _, market, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    venue = ExecVenue(disc.client, pools, refuse=[COIN])
    market.venue = venue
    disc.venue = venue
    disc.screen.wallet = checksum("0x" + "fe" * 20)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert report["added"] == []
        reason = report["rejected"][0]["reason"]
        assert reason.startswith("executable: a buy at the run's order size reverted")
        assert venue.simulated == [(COIN, disc.screen.wallet)]
    finally:
        ledger.close()


def test_without_a_wallet_the_buy_is_not_simulated(tmp_path):
    """Paper runs have no approvals to simulate with; they get no such check."""
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    pools = {v4.pool_id(coin_key()): (10**20, 2**96)}
    ledger, _, market, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})
    venue = ExecVenue(disc.client, pools, refuse=[COIN])
    market.venue = venue
    disc.venue = venue
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
        assert venue.simulated == []
        names = {c["name"] for c in ledger.screen_raw("WOOF")["checks"]}
        assert "executable" not in names
    finally:
        ledger.close()


def test_a_missing_allowance_is_not_held_against_the_token(tmp_path):
    logs = [init_log(USDG, COIN, 30000, 60, PONS_HOOK, 4900)]
    pools = {v4.pool_id(coin_key()): (10**20, 2**96)}
    ledger, _, market, disc = build(tmp_path, logs, {COIN: ("WOOF", 18)})

    class NoAllowance(ExecVenue):
        def swap_call(self, *a, **k):
            from web3.exceptions import ContractLogicError

            from stonkflyrh.v4 import revert_names

            sel = next(s for s, sig in revert_names().items() if sig.startswith("InsufficientAllowance"))

            class Call:
                def call(self, tx):
                    raise ContractLogicError("execution reverted", data=sel + "00" * 32)

            return Call()

    venue = NoAllowance(disc.client, pools)
    market.venue = venue
    disc.venue = venue
    disc.screen.wallet = checksum("0x" + "fe" * 20)
    try:
        report = disc.scan(time.time(), ETH_USD)
        assert [a["symbol"] for a in report["added"]] == ["WOOF"]
        check = next(c for c in ledger.screen_raw("WOOF")["checks"] if c["name"] == "executable")
        assert check["passed"] and "not in place" in check["detail"]
    finally:
        ledger.close()
