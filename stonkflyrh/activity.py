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
