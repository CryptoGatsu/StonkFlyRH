"""Whether anyone is still trading a pool.

The rug screen asks what a token *is*: code, owner, depth, whether it sells
back. None of that says whether the market has moved on. A memecoin whose pool
has not seen a swap in an hour is not a trade, however clean its contract, and
a position in one is a position nobody will take the other side of later.

So the fly reads the pool's own `Swap` events: how many in the recent window,
and how long since the last. Discovery and the screen use the count as a
floor for buying; the tick uses the silence as a reason to leave.
"""

import time

from .chain import checksum
from .config import D

SWAP_V4 = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
SWAP_V3 = "Swap(address,address,int256,int256,uint160,uint128,int24)"


def _tx_hash(log):
    h = log.get("transactionHash")
    if h is None:
        return None
    if isinstance(h, (bytes, bytearray)):
        return "0x" + bytes(h).hex()
    text = str(h)
    return text.lower() if text.startswith("0x") else "0x" + text.lower()


def _swap_sqrt_price(log):
    """sqrtPriceX96 from a v4 Swap event: the third data word (amount0,
    amount1, sqrtPriceX96, liquidity, tick, fee), as a float."""
    data = log.get("data")
    raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
    if len(raw) < 32 * 3:
        return None
    return int.from_bytes(raw[64:96], "big") / 2**96


def swap_topic(signature):
    from eth_utils import keccak

    return "0x" + keccak(text=signature).hex()


class ActivityMonitor:
    """Recent swap counts per product, cached briefly and published for the site."""

    CACHE_SECONDS = 120
    SECONDS_PER_BLOCK_FALLBACK = 0.25

    def __init__(self, settings, client, registry, ledger):
        self.s = settings
        self.client = client
        self.registry = registry
        self.l = ledger
        self.topic_v4 = swap_topic(SWAP_V4)
        self.topic_v3 = swap_topic(SWAP_V3)
        self._cache = {}
        self._spb = None

    # -- chain pace -------------------------------------------------------------

    def seconds_per_block(self):
        if self._spb:
            return self._spb
        try:
            latest = self.client.w3.eth.get_block("latest")
            earlier = self.client.w3.eth.get_block(int(latest["number"]) - 5000)
            spb = (int(latest["timestamp"]) - int(earlier["timestamp"])) / 5000
            self._spb = spb if spb > 0 else self.SECONDS_PER_BLOCK_FALLBACK
        except Exception:
            self._spb = self.SECONDS_PER_BLOCK_FALLBACK
        return self._spb

    # -- reading swaps ----------------------------------------------------------

    def _swap_logs(self, entry, from_block, to_block):
        venue = entry.get("venue", "v3")
        if venue == "v4":
            v4 = self.registry.v4 or {}
            pool_id = entry.get("pool")
            if not v4.get("pool_manager") or not pool_id:
                return None
            params = {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": checksum(v4["pool_manager"]),
                "topics": [self.topic_v4, pool_id],
            }
        else:
            if not entry.get("pool"):
                return None
            params = {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": checksum(entry["pool"]),
                "topics": [self.topic_v3],
            }
        logs, _ = self.client.logs(params, chunk=2000)
        return logs

    def observe(self, product, entry, now=None, window_seconds=None):
        """Swaps in the window and the age of the last one. Cached briefly; the
        result is also written to the ledger's `activity` map for the site."""
        now = time.time() if now is None else now
        window = float(window_seconds or self.s.activity_window_seconds)
        cached = self._cache.get(product)
        if cached and now - cached["checked_at"] < self.CACHE_SECONDS and cached["window"] == window:
            return cached
        head = int(self.client.w3.eth.block_number)
        spb = self.seconds_per_block()
        blocks = max(1, int(window / spb))
        logs = self._swap_logs(entry, max(0, head - blocks), head)
        if logs is None:
            return None
        # The fly's own swaps are not other people trading.
        own = self.l.own_swap_hashes() if hasattr(self.l, "own_swap_hashes") else set()
        if own:
            logs = [l for l in logs if _tx_hash(l) not in own]
        last_block = max((int(l["blockNumber"]) for l in logs), default=None)
        result = {
            "product": product,
            "swaps": len(logs),
            "window_seconds": window,
            "window": window,
            "last_swap_age_seconds": None if last_block is None else max(0.0, (head - last_block) * spb),
            "checked_at": now,
        }
        self._cache[product] = result
        published = dict(self.l.get("activity") or {})
        published[product] = {k: v for k, v in result.items() if k != "window"}
        self.l.put("activity", published)
        return result

    # -- price path -------------------------------------------------------------

    def drawdown(self, product, entry, now=None):
        """How far the token sits below its high over `crash_window_seconds`,
        read from sqrtPriceX96 in the pool's own swap events. None when the
        window holds no swaps or the venue is not v4."""
        if entry.get("venue", "v3") != "v4":
            return None
        now = time.time() if now is None else now
        window = float(self.s.crash_window_seconds)
        key = f"{product}:crash"
        cached = self._cache.get(key)
        if cached and now - cached["checked_at"] < self.CACHE_SECONDS * 4:
            return cached["value"]
        head = int(self.client.w3.eth.block_number)
        blocks = max(1, int(window / self.seconds_per_block()))
        params = {
            "fromBlock": max(0, head - blocks),
            "toBlock": head,
            "address": checksum((self.registry.v4 or {})["pool_manager"]),
            "topics": [self.topic_v4, entry["pool"]],
        }
        logs, _ = self.client.logs(params, chunk=10000)
        series = self._price_series(entry, sorted(logs, key=lambda l: (int(l["blockNumber"]), int(l.get("logIndex", 0)))))
        value = None
        if len(series) >= 2:
            high, last = max(series), series[-1]
            value = {"drawdown": (high - last) / high if high > 0 else 0.0, "swaps": len(series),
                     "window_seconds": window}
        self._cache[key] = {"checked_at": now, "value": value}
        return value

    def _price_series(self, entry, logs):
        """The token's price after each swap, from sqrtPriceX96. sqrtPrice is
        token1 per token0: the token's own price rises with it when the token
        is currency0 and falls with it otherwise."""
        prices = []
        for log in logs:
            try:
                sqrt_p = _swap_sqrt_price(log)
            except (TypeError, ValueError):
                sqrt_p = None
            if sqrt_p:
                prices.append(sqrt_p)
        route = entry.get("route") or []
        key0 = route[-1]["currency0"] if route else None
        token_is_0 = key0 is not None and checksum(key0) == checksum(entry["address"])
        return [p * p for p in prices] if token_is_0 else [1 / (p * p) for p in prices]

    # -- heat: the last few minutes ---------------------------------------------

    HEAT_CACHE_SECONDS = 60

    def heat(self, product, entry, now=None):
        """The pool over the last `hot_window_seconds`: swaps, the age of the
        last one and, for v4, how far the price sits below the window's high
        and where it ended against where it started. Cached briefly and
        published to the ledger's `heat` map for the site and the tick. None
        when the pool cannot be read."""
        now = time.time() if now is None else now
        key = f"{product}:heat"
        cached = self._cache.get(key)
        if cached and now - cached["checked_at"] < self.HEAT_CACHE_SECONDS:
            return cached
        window = float(self.s.hot_window_seconds)
        head = int(self.client.w3.eth.block_number)
        spb = self.seconds_per_block()
        blocks = max(1, int(window / spb))
        logs = self._swap_logs(entry, max(0, head - blocks), head)
        if logs is None:
            return None
        own = self.l.own_swap_hashes() if hasattr(self.l, "own_swap_hashes") else set()
        if own:
            logs = [l for l in logs if _tx_hash(l) not in own]
        logs = sorted(logs, key=lambda l: (int(l["blockNumber"]), int(l.get("logIndex", 0))))
        last_block = int(logs[-1]["blockNumber"]) if logs else None
        result = {
            "product": product,
            "swaps": len(logs),
            "window_seconds": window,
            "last_swap_age_seconds": None if last_block is None else max(0.0, (head - last_block) * spb),
            "drawdown": None,
            "move": None,
            "checked_at": now,
        }
        if entry.get("venue", "v3") == "v4":
            series = self._price_series(entry, logs)
            if len(series) >= 2:
                high = max(series)
                result["drawdown"] = (high - series[-1]) / high if high > 0 else 0.0
                result["move"] = series[-1] / series[0] - 1 if series[0] > 0 else None
        self._cache[key] = result
        published = dict(self.l.get("heat") or {})
        published[product] = result
        self.l.put("heat", published)
        return result

    def warm(self, reading, now=None, max_age=None):
        """Does a heat reading clear the buy thresholds, and is it recent enough
        to trust? `max_age` bounds how old the reading itself may be."""
        if not reading:
            return False
        now = time.time() if now is None else now
        if max_age is not None and now - reading["checked_at"] > max_age:
            return False
        age = reading["last_swap_age_seconds"]
        if reading["swaps"] < int(self.s.min_hot_swaps):
            return False
        if age is None or age > float(self.s.max_last_swap_age_seconds):
            return False
        dd = reading.get("drawdown")
        return dd is None or dd <= float(D(self.s.max_hot_drawdown))

    def buyable(self, product, entry, now=None):
        """(ok, why) for a buy right now. The pool is read afresh; when it
        cannot be, the answer is no: a buy into a pool the fly cannot see is
        not a buy."""
        try:
            h = self.heat(product, entry, now)
        except Exception as e:
            return False, f"could not read the pool's swaps: {type(e).__name__}"
        if h is None:
            return False, "the pool's swaps cannot be read"
        minutes = int(h["window_seconds"] // 60)
        floor = int(self.s.min_hot_swaps)
        age = h["last_swap_age_seconds"]
        if h["swaps"] < floor:
            return False, f"{h['swaps']} swap{'s' if h['swaps'] != 1 else ''} in the last {minutes} min, floor {floor}"
        if age is None:
            return False, f"no swaps in the last {minutes} min"
        limit = float(self.s.max_last_swap_age_seconds)
        if age > limit:
            return False, f"last swap {age / 60:.0f} min ago, limit {limit / 60:.0f} min"
        ceiling = float(D(self.s.max_hot_drawdown))
        if h["drawdown"] is not None and h["drawdown"] > ceiling:
            return False, f"{h['drawdown'] * 100:.0f}% below its {minutes}-min high, ceiling {ceiling * 100:.0f}%"
        return True, f"{h['swaps']} swaps in the last {minutes} min, the last {age / 60:.0f} min ago"

    # -- judgements -------------------------------------------------------------

    def enough(self, product, entry, now=None):
        """(passed, detail, swaps) for the screen's activity check."""
        result = self.observe(product, entry, now)
        if result is None:
            return None
        floor = int(self.s.min_recent_swaps)
        minutes = int(result["window_seconds"] // 60)
        detail = f"{result['swaps']} swap{'s' if result['swaps'] != 1 else ''} in the last {minutes} min, floor {floor}"
        return result["swaps"] >= floor, detail, result["swaps"]

    def exit_reason(self, product, entry, held, now=None):
        """Why a held position should be left: the pool has gone quiet for
        longer than `dead_after_seconds`. None while it is still trading."""
        if held is None or held <= 0:
            return None
        dead_after = float(self.s.dead_after_seconds)
        # Look back a little past the threshold so "no swaps in the window" is
        # the same statement as "quiet for at least dead_after seconds".
        result = self.observe(product, entry, now, window_seconds=dead_after * 1.25)
        if result is None:
            return None
        age = result["last_swap_age_seconds"]
        if age is None:
            return f"no swaps in the last {dead_after / 3600:.1f}h; leaving a dead pool"
        if age >= dead_after:
            return f"last swap {age / 3600:.1f}h ago; leaving a dead pool"
        return None
