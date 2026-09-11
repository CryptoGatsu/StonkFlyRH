"""Heat: what the pool did in the last few minutes, read at the moment of a
buy and used to steer which coin the fly looks at."""

from decimal import Decimal

import pytest

from stonkflyrh.cli import _pick_product
from stonkflyrh.config import D, Settings
from stonkflyrh.risk import Guard, Veto
from tests.test_activity import PM, POOL_ID, SWAP_V4, V4_ENTRY, build, swap_log, swap_topic, swap_with_price


def test_a_pool_swapping_right_now_is_hot(tmp_path):
    # Four blocks a second; the window is 900 s = 3600 blocks back from 100_000.
    logs = [swap_log(b) for b in (97_000, 98_500, 99_900, 99_990)]
    _, ledger, monitor = build(tmp_path, logs, min_volume_usd="0")
    try:
        h = monitor.heat("WOOF", V4_ENTRY, now=1000.0)
        assert h["swaps"] == 4 and h["last_swap_age_seconds"] == pytest.approx(2.5)
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0)
        assert ok and "4 swaps" in why
        assert ledger.get("heat")["WOOF"]["swaps"] == 4
    finally:
        ledger.close()


def test_five_swaps_an_hour_ago_is_not_hot(tmp_path):
    """The screen's hourly floor would pass this pool; the buy gate does not."""
    logs = [swap_log(b) for b in (86_000, 86_100, 86_200, 86_300, 86_400)]
    _, ledger, monitor = build(tmp_path, logs, min_volume_usd="0")
    try:
        assert monitor.enough("WOOF", V4_ENTRY, now=1000.0)[0] is True
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0)
        assert not ok and "0 swaps in the last 15 min" in why
    finally:
        ledger.close()


def test_a_last_swap_too_old_or_a_fresh_collapse_is_not_bought(tmp_path):
    logs = [swap_log(b) for b in (96_500, 96_600, 96_700)]  # last one ~14 min ago
    _, ledger, monitor = build(tmp_path, logs, min_volume_usd="0")
    try:
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0)
        assert not ok and "last swap 14 min ago" in why
    finally:
        ledger.close()
    key = {"currency0": "0x" + "00" * 20, "currency1": V4_ENTRY["address"], "fee": 30000, "tickSpacing": 60, "hooks": "0x" + "00" * 20}
    entry = {**V4_ENTRY, "route": [key]}
    # Token is currency1: a rising sqrtPrice is a falling token price. 1.0 -> 2.0 is -75%.
    logs = [swap_with_price(99_000, 1.0), swap_with_price(99_500, 1.1), swap_with_price(99_990, 2.0)]
    _, ledger, monitor = build(tmp_path / "b", logs, min_volume_usd="0")
    try:
        h = monitor.heat("WOOF", entry, now=1000.0)
        assert h["drawdown"] == pytest.approx(0.75) and h["move"] == pytest.approx(-0.75)
        ok, why = monitor.buyable("WOOF", entry, now=1000.0)
        assert not ok and "75% below its 15-min high" in why
    finally:
        ledger.close()


def test_an_unreadable_pool_is_never_bought(tmp_path):
    _, ledger, monitor = build(tmp_path, [])
    try:
        assert monitor.buyable("WOOF", {**V4_ENTRY, "pool": None}, now=1000.0) == (False, "the pool's swaps cannot be read")
        monitor.client.logs = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("429"))
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0)
        assert not ok and "ConnectionError" in why
    finally:
        ledger.close()


class Cold:
    """An activity monitor that answers no to every buy."""
    HEAT_CACHE_SECONDS = 60

    def __init__(self, why="0 swaps in the last 15 min, floor 3"):
        self.why = why
        self.asked = []

    def buyable(self, product, entry, now=None, price=None):
        self.asked.append(product)
        self.price = price
        return False, self.why


def test_the_guard_vetoes_a_buy_into_a_cold_pool(tmp_path):
    from stonkflyrh.ledger import Ledger
    from stonkflyrh.market import Quote

    settings = Settings(products=("WOOF",))
    ledger = Ledger(tmp_path / "l.sqlite", settings, "paper", D("100"))
    try:
        ledger.commit_tick(ledger.cash, None)
        guard = Guard(settings, ledger, tmp_path / "STOP")
        guard.activity = Cold()
        q = Quote("WOOF", D("1.00"), D("1.01"), 1000.0, 18, 6, 3000, D("1.005"), D("1"))
        with pytest.raises(Veto, match="WOOF is not being traded right now: 0 swaps"):
            guard.plan("WOOF", "BUY", {"WOOF": q}, D("3000"), now=1000.0, gas_price_wei=10**9, history=[1.0] * 5)
        assert guard.activity.asked == ["WOOF"] and guard.activity.price == D("1.005")
    finally:
        ledger.close()


class Map:
    """A monitor whose heat readings come from a dict; counts refreshes."""
    HEAT_CACHE_SECONDS = 60

    def __init__(self, settings, readings):
        self.s = settings
        self.readings = readings
        self.refreshed = []

    def heat(self, product, entry, now=None, price=None):
        self.refreshed.append(product)
        return self.readings.get(product)

    def warm(self, reading, now=None, max_age=None):
        from stonkflyrh.activity import ActivityMonitor
        return ActivityMonitor.warm(self, reading, now, max_age)


class L:
    def __init__(self, tick, positions, heat):
        self.tick, self.positions, self._heat = tick, positions, heat

    def get(self, k):
        return {"tick": self.tick, "heat": self._heat}.get(k)

    def put(self, k, v):
        pass

    def universe(self):
        return {}


def hot(swaps, age, at, volume=9000.0):
    return {"swaps": swaps, "last_swap_age_seconds": age, "drawdown": 0.0, "checked_at": at, "window_seconds": 900,
            "volume_usd": volume, "volume_window_seconds": 300}


def test_the_tick_looks_at_held_coins_and_the_hottest_unheld_ones():
    settings = Settings()
    now = 100_000.0
    heat = {"DEAD": hot(0, None, now - 30), "WARM": hot(3, 120, now - 30), "HOT": hot(9, 10, now - 30),
            "TEPID": hot(4, 300, now - 30), "STALE": hot(50, 5, now - 20_000), "THIN": hot(20, 5, now - 30, volume=900.0)}
    observable = ["DEAD", "WARM", "HOT", "TEPID", "STALE", "HELD", "UNREAD", "THIN"]
    ledger = L(0, {"HELD": Decimal("5")}, heat)
    monitor = Map(settings, {"UNREAD": hot(1, 700, now)})
    seen = [_pick_product(observable, L(t, ledger.positions, heat), monitor, settings, now) for t in range(4)]
    # Held first, then the hottest three; the dead, the stale reading and the busy-but-thin pool are not shown.
    assert seen == ["HELD", "HOT", "TEPID", "WARM"]
    # The unread coin was the one refreshed, once per tick.
    assert monitor.refreshed == ["UNREAD"] * 4
    # Nothing warm and nothing held: watch the liveliest reading; the gate decides buys.
    cold = {"A": hot(1, 800, now - 30), "B": hot(2, 900, now - 30)}
    assert _pick_product(["A", "B"], L(0, {}, cold), Map(settings, {}), settings, now) == "B"
    # Without activity tracking the old rotation stands.
    assert _pick_product(["A", "B"], L(1, {}, {}), None, settings, now) == "B"


def swap_with_amounts(block, amount0, amount1=0):
    data = amount0.to_bytes(32, "big", signed=True) + amount1.to_bytes(32, "big", signed=True) + bytes(32) * 4
    return {"address": PM, "topics": [swap_topic(SWAP_V4), POOL_ID], "blockNumber": block, "logIndex": 0, "data": data}


def test_a_buy_needs_dollars_through_the_pool_in_the_last_five_minutes(tmp_path):
    """Four swaps of two million tokens each in the last five minutes (1200
    blocks), one earlier that does not count. At $0.001 that is $8,000; at
    $0.0002 it is $1,600 and the floor is $3,000. TOKEN is the lower address,
    so it is currency0 and amount0 is the token leg."""
    unit = 10**18
    logs = [swap_with_amounts(98_000, 50_000_000 * unit)] + [
        swap_with_amounts(b, -2_000_000 * unit if i % 2 else 2_000_000 * unit) for i, b in enumerate((99_000, 99_300, 99_600, 99_950))
    ]
    _, ledger, monitor = build(tmp_path, logs)
    try:
        h = monitor.heat("WOOF", V4_ENTRY, now=1000.0, price=D("0.001"))
        assert h["swaps"] == 5 and h["volume_swaps"] == 4 and h["volume_usd"] == pytest.approx(8000.0)
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0, price=D("0.001"))
        assert ok and "$8,000 in the last 5 min" in why
        assert ledger.get("heat")["WOOF"]["volume_usd"] == pytest.approx(8000.0)
        monitor._cache.clear()
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0, price=D("0.0002"))
        assert not ok and why == "$1,600 traded in the last 5 min, floor $3,000"
        monitor._cache.clear()
        ok, why = monitor.buyable("WOOF", V4_ENTRY, now=1000.0)
        assert not ok and why == "volume in the last 5 min cannot be priced"
        # A cached reading taken without a price is priced once a price arrives.
        priced = monitor.heat("WOOF", V4_ENTRY, now=1010.0, price=D("0.001"))
        assert priced["volume_usd"] == pytest.approx(8000.0)
        # Thin volume alone keeps a busy pool out of the lineup.
        assert monitor.warm({**priced, "volume_usd": 2999.0}, now=1010.0) is False
        assert monitor.warm(priced, now=1010.0) is True
    finally:
        ledger.close()
