"""Is anyone here? Swap counts from the pool's own events, the buy floor, the
market-cap floor, and leaving a pool that has gone quiet."""


from stonkflyrh.activity import SWAP_V3, SWAP_V4, ActivityMonitor, swap_topic
from stonkflyrh.chain import checksum
from stonkflyrh.config import D, Settings
from stonkflyrh.ledger import Ledger

PM = checksum("0x" + "9a" * 20)
POOL_ID = "0x" + "ab" * 32
V3_POOL = checksum("0x" + "5c" * 20)
TOKEN = checksum("0x" + "0a" * 20)


def swap_log(block, v4=True):
    if v4:
        return {"address": PM, "topics": [swap_topic(SWAP_V4), POOL_ID], "blockNumber": block}
    return {"address": V3_POOL, "topics": [swap_topic(SWAP_V3)], "blockNumber": block}


class Eth:
    def __init__(self, logs, head):
        self.logs = logs
        self.block_number = head
        self.queries = []

    def get_block(self, tag):
        # Four blocks a second.
        number = self.block_number if tag == "latest" else int(tag)
        return {"number": number, "timestamp": 1_700_000_000 + number // 4}

    def get_logs(self, q):
        self.queries.append(q)
        want = checksum(q["address"])
        return [l for l in self.logs
                if q["fromBlock"] <= l["blockNumber"] <= q["toBlock"]
                and checksum(l["address"]) == want
                and all(t is None or str(l["topics"][i]).lower() == str(t).lower() for i, t in enumerate(q["topics"]))]


class Client:
    def __init__(self, logs, head=100_000):
        self.w3 = type("W3", (), {})()
        self.w3.eth = Eth(logs, head)

    def logs(self, params, chunk=2000, pause=0, retries=6, max_blocks=None):
        return self.w3.eth.get_logs(params), int(params["toBlock"])


class Registry:
    v4 = {"pool_manager": PM}
    quote_address = checksum("0x" + "11" * 20)
    quote_decimals = 6


def build(tmp_path, logs, head=100_000, **overrides):
    settings = Settings(**overrides)
    ledger = Ledger(tmp_path / "l.sqlite", settings, "paper", D("100"))
    return settings, ledger, ActivityMonitor(settings, Client(logs, head), Registry(), ledger)


V4_ENTRY = {"symbol": "WOOF", "address": TOKEN, "venue": "v4", "pool": POOL_ID, "decimals": 18}
V3_ENTRY = {"symbol": "WOOF", "address": TOKEN, "venue": "v3", "pool": V3_POOL, "decimals": 18}


def test_swaps_in_the_window_are_counted_from_the_pools_own_events(tmp_path):
    # 4 blocks/s: an hour is 14,400 blocks. Six swaps inside, two long before.
    logs = [swap_log(100_000 - 100 * i) for i in range(6)] + [swap_log(50_000), swap_log(40_000)]
    _, ledger, monitor = build(tmp_path, logs)
    try:
        result = monitor.observe("WOOF", V4_ENTRY, now=1000.0)
        assert result["swaps"] == 6
        assert result["last_swap_age_seconds"] == 0.0
        assert ledger.get("activity")["WOOF"]["swaps"] == 6           # published for the site
        q = monitor.client.w3.eth.queries[-1]
        assert q["topics"] == [swap_topic(SWAP_V4), POOL_ID] and q["fromBlock"] == 100_000 - 14_400
        # Cached: a second look within two minutes does not ask the chain again.
        asked = len(monitor.client.w3.eth.queries)
        monitor.observe("WOOF", V4_ENTRY, now=1060.0)
        assert len(monitor.client.w3.eth.queries) == asked
    finally:
        ledger.close()


def test_a_v3_pool_is_read_from_its_own_address(tmp_path):
    _, ledger, monitor = build(tmp_path, [swap_log(99_990, v4=False)])
    try:
        result = monitor.observe("WOOF", V3_ENTRY, now=1000.0)
        assert result["swaps"] == 1 and monitor.client.w3.eth.queries[-1]["address"] == V3_POOL
    finally:
        ledger.close()


def test_the_buy_floor_needs_recent_swaps(tmp_path):
    _, ledger, monitor = build(tmp_path, [swap_log(99_999)], min_recent_swaps=5)
    try:
        passed, detail, swaps = monitor.enough("WOOF", V4_ENTRY, now=1000.0)
        assert not passed and swaps == 1 and "floor 5" in detail
    finally:
        ledger.close()


def test_a_quiet_pool_is_left_and_an_active_one_is_kept(tmp_path):
    # Last swap 5 hours ago (72,000 blocks at 4/s); dead after 4 hours.
    _, ledger, monitor = build(tmp_path, [swap_log(100_000 - 72_000)], dead_after_seconds=14_400)
    try:
        why = monitor.exit_reason("WOOF", V4_ENTRY, D("1000"), now=1000.0)
        assert why and "dead pool" in why and "5.0h" in why
        assert monitor.exit_reason("WOOF", V4_ENTRY, D("0"), now=1000.0) is None   # nothing held
    finally:
        ledger.close()
    _, ledger, monitor = build(tmp_path / "b", [swap_log(100_000 - 4_000)], dead_after_seconds=14_400)
    try:
        assert monitor.exit_reason("WOOF", V4_ENTRY, D("1000"), now=1000.0) is None
    finally:
        ledger.close()


def test_no_swaps_at_all_in_the_window_also_means_leaving(tmp_path):
    _, ledger, monitor = build(tmp_path, [], dead_after_seconds=14_400)
    try:
        why = monitor.exit_reason("WOOF", V4_ENTRY, D("1"), now=1000.0)
        assert why and "no swaps" in why
    finally:
        ledger.close()
