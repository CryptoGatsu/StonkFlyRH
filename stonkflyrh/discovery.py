"""Finding new memecoins to trade, from the chain itself.

Pons and every other launchpad on Robinhood Chain ends the same way: a Uniswap
pool. So discovery watches the one place they all arrive — the v3 factory's
`PoolCreated` events — and takes every new pool that pairs a token against the
quote asset (USDG). Each candidate goes through the same rug screen a hand-picked
token would, and only a token the screen approves joins the universe the fly
trades. Nothing here is a strategy: discovery decides what the fly may *see*,
the screen decides what it may *buy*, and the connectome decides whether it does.

State lives in the ledger: the last block scanned, the universe, and every
candidate ever screened with its verdict, so a restart neither re-screens the
same pool nor forgets a rejection.
"""

import re

from .chain import checksum
from .config import D, POOL_FEE_TIERS, SYMBOL

POOL_CREATED = "PoolCreated(address,address,uint24,int24,address)"
CLEAN = re.compile(r"[^A-Z0-9]")


def pool_created_topic():
    from eth_utils import keccak

    return "0x" + keccak(text=POOL_CREATED).hex()


def topic_address(topic):
    raw = bytes(topic) if not isinstance(topic, str) else bytes.fromhex(topic[2:])
    return checksum("0x" + raw[-20:].hex())


def topic_int(topic):
    raw = bytes(topic) if not isinstance(topic, str) else bytes.fromhex(topic[2:])
    return int.from_bytes(raw, "big")


def decode_pool_created(log):
    """token0, token1, fee, pool from one factory log."""
    topics = log["topics"]
    data = log["data"]
    raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
    if len(topics) != 4 or len(raw) < 64:
        raise ValueError("Not a PoolCreated log")
    return {
        "token0": topic_address(topics[1]),
        "token1": topic_address(topics[2]),
        "fee": topic_int(topics[3]),
        "pool": checksum("0x" + raw[32:64][-20:].hex()),
        "block": int(log["blockNumber"]),
    }


def clean_symbol(raw, address, taken):
    """A tradable symbol: uppercase alphanumerics, unique within the universe.

    Memecoins reuse and decorate their tickers. The on-chain symbol is kept as
    the display name; the key the run uses is normalised, and a collision gets
    the first bytes of the address appended so two PEPEs stay two tokens.
    """
    base = CLEAN.sub("", str(raw or "").upper())[:10] or "TOKEN"
    if len(base) < 2:
        base = (base + "XX")[:2]
    key = base
    if key in taken:
        key = f"{base}_{address[2:6].upper()}"
    if not SYMBOL.match(key):
        key = f"T_{address[2:10].upper()}"
    return key


class PoolDiscovery:
    """Scans factory logs for new quote-asset pools and screens what it finds."""

    CHUNK = 4000

    def __init__(self, settings, client, registry, ledger, market, screen, factory):
        self.s = settings
        self.client = client
        self.registry = registry
        self.l = ledger
        self.market = market
        self.screen = screen
        self.factory = checksum(factory)
        self.topic = pool_created_topic()
        self.last_scan = 0.0

    def due(self, now):
        return now - self.last_scan >= self.s.discovery_interval_seconds

    def scan(self, now, eth_usd):
        self.last_scan = now
        head = int(self.client.w3.eth.block_number)
        start = self.l.get("discovery_block")
        if start is None:
            start = max(0, head - int(self.s.discovery_lookback_blocks))
        report = {
            "at": now,
            "from_block": start,
            "to_block": head,
            "candidates": 0,
            "added": [],
            "rejected": [],
            "skipped": 0,
        }
        if head <= start:
            return report
        logs = []
        for lo in range(start + 1, head + 1, self.CHUNK):
            hi = min(head, lo + self.CHUNK - 1)
            logs += self.client.w3.eth.get_logs(
                {
                    "fromBlock": lo,
                    "toBlock": hi,
                    "address": self.factory,
                    "topics": [self.topic],
                }
            )
        candidates = []
        for log in logs:
            try:
                created = decode_pool_created(log)
            except (ValueError, KeyError):
                continue
            if created["fee"] not in POOL_FEE_TIERS:
                continue
            quote = self.registry.quote_address
            if quote not in (created["token0"], created["token1"]):
                continue
            other = created["token1"] if created["token0"] == quote else created["token0"]
            if other in (quote, self.registry.weth):
                continue
            candidates.append({**created, "token": other})
        # Newest first; a pool the run already knows about is not a candidate.
        candidates.sort(key=lambda c: -c["block"])
        universe = self.l.universe()
        known = {e.get("address") for e in universe.values()} | set(self.l.seen_candidates())
        fresh = [c for c in candidates if c["token"] not in known]
        report["candidates"] = len(fresh)
        report["skipped"] = len(candidates) - len(fresh)
        room = max(0, int(self.s.max_products) - len(universe))
        for c in fresh[: int(self.s.discovery_batch)]:
            if room <= 0:
                report["rejected"].append({"address": c["token"], "reason": "universe full"})
                self.l.mark_candidate(c["token"], "universe full", now)
                continue
            outcome = self._consider(c, now, eth_usd)
            if outcome["added"]:
                room -= 1
                report["added"].append(outcome)
            else:
                report["rejected"].append(outcome)
        self.l.put("discovery_block", head)
        self.l.record_discovery(report)
        return report

    def _consider(self, created, now, eth_usd):
        address = created["token"]
        try:
            identity = self.client.token_identity(address)
        except Exception as e:
            self.l.mark_candidate(address, f"unreadable token: {type(e).__name__}", now)
            return {"address": address, "added": False, "reason": "unreadable token"}
        symbol = clean_symbol(identity["symbol"], address, set(self.l.universe()))
        if self.l.is_blocked(symbol):
            self.l.mark_candidate(address, "blocklisted symbol", now)
            return {"address": address, "symbol": symbol, "added": False, "reason": "blocklisted"}
        entry = {
            "symbol": symbol,
            "name": str(identity["symbol"])[:32],
            "address": address,
            "decimals": int(identity["decimals"]),
            "pool_fee": int(created["fee"]),
            "pool": created["pool"],
            "source": "factory:PoolCreated",
            "discovered_block": created["block"],
            "added_at": now,
        }
        self.registry.add_token(entry)
        self.market.add_product(symbol, entry["pool"], entry["pool_fee"])
        verdict = self.screen.assess(symbol, entry["pool"], eth_usd, now, force=True)
        if not verdict.approved:
            self.registry.remove_token(symbol)
            self.market.remove_product(symbol)
            self.l.mark_candidate(address, verdict.reason(), now)
            return {
                "address": address,
                "symbol": symbol,
                "added": False,
                "reason": verdict.reason(),
            }
        self.l.add_to_universe(entry)
        self.l.mark_candidate(address, "added", now)
        return {"address": address, "symbol": symbol, "added": True, "pool": entry["pool"]}

    def prune(self, now, eth_usd):
        """Drop an unheld discovered token that no longer clears the screen."""
        dropped = []
        held = self.l.positions
        for symbol, entry in list(self.l.universe().items()):
            if entry.get("source") == "seed" or held.get(symbol, D(0)) > 0:
                continue
            verdict = self.screen.assess(symbol, entry["pool"], eth_usd, now)
            if not verdict.approved:
                self.l.remove_from_universe(symbol, verdict.reason(), now)
                self.registry.remove_token(symbol)
                self.market.remove_product(symbol)
                dropped.append({"symbol": symbol, "reason": verdict.reason()})
        return dropped


class FixtureDiscovery:
    """Offline stand-in: adds one synthetic token on the third scan."""

    def __init__(self, settings, ledger, market):
        self.s = settings
        self.l = ledger
        self.market = market
        self.last_scan = 0.0
        self.scans = 0

    def due(self, now):
        return now - self.last_scan >= self.s.discovery_interval_seconds

    def scan(self, now, eth_usd):
        self.last_scan = now
        self.scans += 1
        report = {"at": now, "candidates": 0, "added": [], "rejected": [], "fixture": True}
        if self.scans == 3 and "NEWCOIN" not in self.l.universe():
            entry = {
                "symbol": "NEWCOIN",
                "name": "NEWCOIN",
                "address": "0x" + "ee" * 20,
                "decimals": 18,
                "pool_fee": self.s.pool_fee_tier,
                "pool": "0x" + "ef" * 20,
                "source": "fixture",
                "discovered_block": 0,
                "added_at": now,
            }
            self.l.add_to_universe(entry)
            self.market.add_product("NEWCOIN", entry["pool"], entry["pool_fee"])
            report["candidates"] = 1
            report["added"].append({"symbol": "NEWCOIN", "added": True, "pool": entry["pool"]})
        self.l.record_discovery(report)
        return report

    def prune(self, now, eth_usd):
        return []
