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
from . import v4 as v4mod

# v4 fees are arbitrary; this flag marks a hook-set dynamic fee.
V4_DYNAMIC_FEE = 0x800000
V4_MAX_FEE = 100_000  # 10%
# A plain (hookless) pool at a standard tier is a place to route *through*.
BRIDGE_MAX_FEE = 3000

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

    CHUNK = 2000
    # Blocks covered by one scan, per venue. The first scan of a fresh run has a
    # 400k-block backlog; spreading it keeps each scan to a handful of requests
    # against a public RPC that rate-limits.
    MAX_BLOCKS_PER_SCAN = 30000
    # Progress is written to the ledger after every window, so a rate-limit
    # error part-way through a scan costs at most one window, not the scan.
    WINDOW = 6000
    # A pool initialised without liquidity is looked at again this many times,
    # min_pool_age_seconds apart, before the token is given up on.
    MAX_EMPTY_POOL_RETRIES = 48

    def __init__(self, settings, client, registry, ledger, market, screen, factory, venue=None):
        self.s = settings
        self.client = client
        self.registry = registry
        self.l = ledger
        self.market = market
        self.screen = screen
        self.factory = checksum(factory)
        self.topic = pool_created_topic()
        self.venue = venue
        self.v4_topic = v4mod.initialize_topic()
        self.last_scan = 0.0
        if venue is not None:
            self._seed_bridges()

    # -- bridges: currencies with a known v4 route back to USDG ---------------

    def _seed_bridges(self):
        """Registry-declared bridges (GOOGL, WETH, ...) come first."""
        bridges = self.l.get("bridges") or {}
        for b in (self.registry.v4 or {}).get("bridges", []):
            pool = b["pool"]
            if v4mod.NATIVE in (checksum(pool["currency0"]), checksum(pool["currency1"])):
                # An unfilled slot in the registry, not a pool. Leave it alone.
                continue
            key = v4mod.pool_key(
                pool["currency0"], pool["currency1"], pool["fee"], pool["tickSpacing"], pool["hooks"]
            )
            if self.registry.quote_address not in (key["currency0"], key["currency1"]):
                self.l.record_discovery(
                    {"at": 0, "bridge_ignored": b.get("symbol"), "reason": "not a USDG pool"}
                )
                continue
            other = key["currency1"] if key["currency0"] == self.registry.quote_address else key["currency0"]
            bridges[other] = {"symbol": b.get("symbol", other[:8]), "route": [key], "source": "registry"}
        if bridges:
            self.l.put("bridges", bridges)
        # The USDG/ETH and USDG/WETH pools are older than any lookback; ask the
        # StateView for them directly rather than wait for their logs.
        for currency, symbol in ((v4mod.NATIVE, "ETH"), (self.registry.weth, "WETH")):
            if currency and currency not in self.bridges():
                self._probe_bridge(currency, symbol)

    # Standard hookless (fee, tickSpacing) tiers a plain USDG pool would use.
    BRIDGE_TIERS = ((100, 1), (500, 10), (3000, 60))
    BRIDGE_REPROBE_BLOCKS = 20000

    def _probe_bridge(self, currency, symbol=None):
        """Look for a hookless USDG pool for `currency` at a standard tier by
        reading pool state, and learn the deepest one as a bridge. Negative
        results are remembered for a while so a scan does not re-ask."""
        if self.venue is None or not hasattr(self.venue, "liquidity"):
            return None
        currency = checksum(currency)
        quote = self.registry.quote_address
        if currency == quote or currency in self.bridges():
            return self.bridges().get(currency)
        probes = dict(self.l.get("bridge_probes") or {})
        try:
            head = int(self.client.w3.eth.block_number)
        except Exception:
            head = 0
        last = probes.get(currency)
        if last is not None and head - int(last) < self.BRIDGE_REPROBE_BLOCKS:
            return None
        best = None
        for fee, spacing in self.BRIDGE_TIERS:
            a, b = sorted((currency, quote), key=lambda x: int(x, 16))
            key = v4mod.pool_key(a, b, fee, spacing, v4mod.NATIVE)
            try:
                liquidity = int(self.venue.liquidity(key))
            except Exception:
                continue
            if liquidity > 0 and (best is None or liquidity > best[0]):
                best = (liquidity, key)
        probes[currency] = head
        self.l.put("bridge_probes", probes)
        if best is None:
            return None
        bridges = self.bridges()
        bridges[currency] = {
            "symbol": symbol or ("ETH" if currency == v4mod.NATIVE else currency[:8]),
            "route": [best[1]],
            "source": "probed",
            "block": head,
        }
        self.l.put("bridges", bridges)
        return bridges[currency]

    def bridges(self):
        return self.l.get("bridges") or {}

    def _learn_bridge(self, created, other):
        """A hookless USDG pool at a standard tier is a route others can use.
        That includes USDG/ETH: native ETH is a currency v4 can route through."""
        if created["hooks"] != v4mod.NATIVE or created["fee"] > BRIDGE_MAX_FEE:
            return
        bridges = self.bridges()
        if other in bridges:
            return
        key = v4mod.pool_key(created["currency0"], created["currency1"], created["fee"], created["tickSpacing"], created["hooks"])
        symbol = "ETH" if other == v4mod.NATIVE else other[:8]
        bridges[other] = {"symbol": symbol, "route": [key], "source": "observed", "block": created["block"]}
        self.l.put("bridges", bridges)

    def _hook_allowed(self, hooks):
        conf = self.registry.v4 or {}
        if hooks == v4mod.NATIVE or conf.get("hooks_allow_any"):
            return True
        return checksum(hooks) in conf.get("hooks_allow", [])

    def scan(self, now, eth_usd):
        """Advance the log scan, queue what it finds, screen a batch.

        Two phases, each persisted as it goes. Fetching walks the chain in
        windows and records the block reached after every window, so an RPC
        error mid-scan resumes from the last window rather than the start.
        Screening takes candidates from a queue in the ledger, one at a time,
        so a candidate whose screen failed on a network error is still queued
        on the next scan.
        """
        import time as _time

        self.last_scan = now
        budget = float(getattr(self.s, "discovery_budget_seconds", 25))
        self._deadline = _time.monotonic() + budget
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
        if head > start:
            # Fetching may use half the budget; screening gets the rest.
            self._fetch_deadline = _time.monotonic() + budget / 2
            scanned_to = self._fetch(start, head, report)
            report["to_block"] = scanned_to
            report["backlog_blocks"] = head - scanned_to
        else:
            report["backlog_blocks"] = 0
        self._screen_pending(now, eth_usd, report)
        report["readmitted"] = self.readmit(now, eth_usd)
        report["seconds"] = round(budget - (self._deadline - _time.monotonic()), 1)
        report["pending"] = len(self.l.get("pending_candidates") or [])
        self.l.record_discovery(report)
        return report

    def _fetch(self, start, head, report):
        quote = self.registry.quote_address
        import time as _time

        limit = min(head, start + self.MAX_BLOCKS_PER_SCAN)
        scanned_to = start
        fetch_deadline = getattr(self, "_fetch_deadline", None)
        while scanned_to < limit:
            if fetch_deadline is not None and scanned_to > start and _time.monotonic() > fetch_deadline:
                break  # out of time for this scan; the rest is next scan's
            lo = scanned_to + 1
            hi = min(limit, scanned_to + self.WINDOW)
            candidates = []
            if self.s.discover_v3:
                logs, hi3 = self.client.logs(
                    {"fromBlock": lo, "toBlock": hi, "address": self.factory, "topics": [self.topic]},
                    chunk=self.CHUNK,
                )
                hi = min(hi, hi3)
                for log in logs:
                    try:
                        created = decode_pool_created(log)
                    except (ValueError, KeyError):
                        continue
                    if created["fee"] not in POOL_FEE_TIERS:
                        continue
                    if quote not in (created["token0"], created["token1"]):
                        continue
                    other = created["token1"] if created["token0"] == quote else created["token0"]
                    if other in (quote, self.registry.weth):
                        continue
                    candidates.append({**created, "token": other, "venue": "v3"})
            if self.s.discover_v4 and self.venue is not None:
                logs, hi4 = self.client.logs(
                    {
                        "fromBlock": lo,
                        "toBlock": hi,
                        "address": self.venue.pool_manager,
                        "topics": [self.v4_topic],
                    },
                    chunk=self.CHUNK,
                )
                hi = min(hi, hi4)
                # Pools that only become reachable once a bridge exists wait here.
                unrouted = list(self.l.get("unrouted_v4") or [])
                for log in logs:
                    try:
                        created = v4mod.decode_initialize(log)
                    except (ValueError, KeyError):
                        continue
                    unrouted.append(created)
                # Learn every bridge in the batch first, so a pool that arrived
                # before its bridge did is routed in the same scan.
                for created in unrouted[-400:]:
                    if quote in (created["currency0"], created["currency1"]):
                        other = created["currency1"] if created["currency0"] == quote else created["currency0"]
                        if other != quote:
                            self._learn_bridge(created, other)
                still = []
                for created in unrouted[-400:]:
                    c = self._route_v4(created, quote)
                    if c is None:
                        still.append(created)
                    elif c:
                        candidates.append(c)
                self.l.put("unrouted_v4", still[-200:])
            self._enqueue(candidates, report)
            scanned_to = hi
            with self.l.transaction():
                self.l.put("discovery_block", scanned_to)
                self.l.put("discovery_backlog", head - scanned_to)
        return scanned_to

    def _enqueue(self, candidates, report):
        """Queue the pools this window found that the run has not seen before."""
        pending = list(self.l.get("pending_candidates") or [])
        universe = self.l.universe()
        known = (
            {e.get("address") for e in universe.values()}
            | set(self.l.seen_candidates())
            | {c["token"] for c in pending}
        )
        # Newest first; the same token launching twice is one candidate.
        candidates.sort(key=lambda c: -c["block"])
        for c in candidates:
            if c["token"] in known:
                report["skipped"] += 1
                continue
            known.add(c["token"])
            pending.append(c)
            report["candidates"] += 1
        self.l.put("pending_candidates", pending[-500:])

    def _screen_pending(self, now, eth_usd, report):
        pending = list(self.l.get("pending_candidates") or [])
        if not pending:
            return
        universe = self.l.universe()
        room = max(0, int(self.s.max_products) - len(universe))
        coin = checksum(self.s.coin_address) if self.s.coin_address else None
        # Candidates waiting for their pool to fill are not due yet.
        ready = [c for c in pending if c.get("not_before", 0) <= now]
        waiting = [c for c in pending if c.get("not_before", 0) > now]
        report["waiting_for_liquidity"] = len(waiting)
        # The operator's own coin is looked at first and is not subject to the
        # cap; otherwise the newest launch first.
        ready.sort(key=lambda c: (0 if c["token"] == coin else 1, -c["block"]))
        batch, rest = ready[: int(self.s.discovery_batch)], ready[int(self.s.discovery_batch):]
        import time as _time

        deadline = getattr(self, "_deadline", None)
        retried = []
        for i, c in enumerate(batch):
            if deadline is not None and i > 0 and _time.monotonic() > deadline:
                # Out of time: everything from here stays queued for next scan.
                self.l.put("pending_candidates", (batch[i:] + rest + waiting + retried)[-500:])
                report["deferred"] = len(batch) - i
                return
            if room <= 0 and c["token"] != coin:
                report["rejected"].append({"address": c["token"], "reason": "universe full"})
                self.l.mark_candidate(c["token"], "universe full", now)
            else:
                # A network error here propagates; this candidate and the ones
                # after it stay queued for the next scan.
                outcome = self._consider(c, now, eth_usd)
                if outcome["added"]:
                    if c["token"] != coin:
                        room -= 1
                    report["added"].append(outcome)
                elif outcome.get("retry"):
                    tries = int(c.get("retries", 0)) + 1
                    if tries <= self.MAX_EMPTY_POOL_RETRIES:
                        retried.append({**c, "retries": tries,
                                        "not_before": now + float(self.s.min_pool_age_seconds)})
                        report.setdefault("retried", []).append(outcome)
                    else:
                        self.l.mark_candidate(c["token"], "pool never received liquidity", now)
                        report["rejected"].append({**outcome, "reason": "pool never received liquidity"})
                else:
                    report["rejected"].append(outcome)
            self.l.put("pending_candidates", (batch[i + 1:] + rest + waiting + retried)[-500:])

    # Rejection texts produced by checks that were later found to be broken.
    # A candidate turned away for one of these was never really judged.
    STALE_REJECTIONS = ("pool creation block unknown",)

    def heal(self, now):
        """Forget rejections that a since-fixed check produced, and rewind the
        scan so those pools are looked at again with the corrected screen.
        Runs once per ledger; a run that never hit the bug does nothing."""
        if self.l.get("healed_stale_rejections"):
            return None
        cleared = 0
        for text in self.STALE_REJECTIONS:
            cleared += self.l.db.execute(
                "DELETE FROM candidates WHERE outcome LIKE ?", (f"%{text}%",)
            ).rowcount
        report = {"at": now, "cleared_stale_rejections": cleared}
        if cleared:
            head = int(self.client.w3.eth.block_number)
            start = max(0, head - int(self.s.discovery_lookback_blocks))
            with self.l.transaction():
                self.l.put("discovery_block", start)
                self.l.put("discovery_backlog", head - start)
            report["rescan_from_block"] = start
        self.l.put("healed_stale_rejections", True)
        self.l.record_event("migration", report)
        return report

    def heal_lost_drops(self, now):
        """Tokens evicted before drops were recorded are marked 'added' in the
        candidates table yet sit in neither the universe nor the dropped list,
        so the scan skips them for good. Forget those marks and rewind once."""
        if self.l.get("healed_lost_drops"):
            return None
        present = {checksum(e["address"]) for e in self.l.universe().values() if e.get("address")}
        present |= {checksum(e["address"]) for e in self.l.dropped().values() if e.get("address")}
        lost = [
            r[0] for r in self.l.db.execute("SELECT address FROM candidates WHERE outcome='added'")
            if checksum(r[0]) not in present
        ]
        for address in lost:
            self.l.db.execute("DELETE FROM candidates WHERE address=?", (address,))
        report = {"at": now, "forgotten_lost_drops": len(lost)}
        if lost:
            head = int(self.client.w3.eth.block_number)
            start = max(0, head - int(self.s.discovery_lookback_blocks))
            with self.l.transaction():
                self.l.put("discovery_block", start)
                self.l.put("discovery_backlog", head - start)
            report["rescan_from_block"] = start
        self.l.put("healed_lost_drops", True)
        self.l.record_event("migration", report)
        return report

    def due(self, now):
        # With a backlog still to cover, or candidates still queued, scan again
        # soon rather than in ten minutes.
        behind = (self.l.get("discovery_backlog") or 0) or self.l.get("pending_candidates")
        interval = 60 if behind else self.s.discovery_interval_seconds
        return now - self.last_scan >= interval

    def _route_v4(self, created, quote):
        """A v4 pool becomes a candidate when USDG can reach its token.

        Returns a candidate dict, None to keep waiting for a bridge, or False
        to drop it for good.
        """
        c0, c1 = created["currency0"], created["currency1"]
        fee = created["fee"]
        if fee != V4_DYNAMIC_FEE and fee > V4_MAX_FEE:
            return False
        if not self._hook_allowed(created["hooks"]):
            return False
        key = v4mod.pool_key(c0, c1, fee, created["tickSpacing"], created["hooks"])
        if quote in (c0, c1):
            other = c1 if c0 == quote else c0
            if other == self.registry.weth or other in self.bridges():
                # Plain USDG pools for known assets are routes, not memecoins.
                self._learn_bridge(created, other)
                return False
            self._learn_bridge(created, other)
            if created["hooks"] == v4mod.NATIVE and fee <= BRIDGE_MAX_FEE:
                return False  # a standard hookless pool is a bridge, not a launch
            return {**created, "token": other, "venue": "v4", "route": [key], "pool": v4mod.pool_id(key)}
        bridges = self.bridges()
        for side, other in ((c0, c1), (c1, c0)):
            if side in bridges:
                route = [dict(k) for k in bridges[side]["route"]] + [key]
                return {**created, "token": other, "venue": "v4", "route": route,
                        "pool": v4mod.pool_id(key), "via": bridges[side].get("symbol")}
        # Neither side is known. One of them may have a plain USDG pool that
        # predates the scan (GOOGL, say): ask for it once in a while.
        for side in (c0, c1):
            if side not in bridges and self._probe_bridge(side):
                return self._route_v4(created, quote)
        return None

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
        venue = created.get("venue", "v3")
        entry = {
            "symbol": symbol,
            "name": str(identity["symbol"])[:32],
            "address": address,
            "decimals": int(identity["decimals"]),
            "pool_fee": int(created["fee"]),
            "pool": created["pool"],
            "venue": venue,
            "route": created.get("route"),
            "hooks": created.get("hooks"),
            "via": created.get("via"),
            "source": "factory:PoolCreated" if venue == "v3" else "v4:Initialize",
            "discovered_block": created["block"],
            "added_at": now,
            "total_supply": self._total_supply(address),
        }
        self.registry.add_token(entry)
        self.market.add_product(symbol, entry["pool"], entry["pool_fee"], venue, entry.get("route"))
        verdict = self.screen.assess(symbol, entry["pool"], eth_usd, now, force=True)
        if not verdict.tradeable:
            self.registry.remove_token(symbol)
            self.market.remove_product(symbol)
            if verdict.retry:
                # Not a judgement on the token: the pool has no liquidity yet.
                return {"address": address, "symbol": symbol, "added": False,
                        "reason": verdict.reason(), "retry": True}
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

    # A dropped token is screened again every screen_ttl_seconds, this many
    # times, before it is given up on.
    MAX_READMIT_TRIES = 48

    def readmit(self, now, eth_usd):
        """Look again at tokens the screen dropped, once their verdict has aged;
        a token that clears rejoins the universe with its route intact."""
        import time as _time

        deadline = getattr(self, "_deadline", None)
        dropped = self.l.dropped()
        if not dropped:
            return []
        universe = self.l.universe()
        room = max(0, int(self.s.max_products) - len(universe))
        back = []
        changed = False
        for symbol, entry in sorted(dropped.items(), key=lambda kv: kv[1].get("dropped_at", 0)):
            if deadline is not None and _time.monotonic() > deadline:
                break
            if now - float(entry.get("dropped_at", 0)) < float(self.s.screen_ttl_seconds):
                continue
            if symbol in universe or self.l.is_blocked(symbol):
                dropped.pop(symbol, None)
                changed = True
                continue
            if room <= 0:
                break
            self.registry.add_token(entry)
            self.market.add_product(symbol, entry["pool"], entry["pool_fee"], entry.get("venue", "v3"), entry.get("route"))
            verdict = self.screen.assess(symbol, entry["pool"], eth_usd, now, force=True)
            if verdict.tradeable:
                clean = {k: v for k, v in entry.items() if k not in ("reason", "dropped_at", "drops")}
                clean["readmitted_at"] = now
                self.l.add_to_universe(clean)
                dropped.pop(symbol, None)
                room -= 1
                back.append({"symbol": symbol, "after_drops": entry.get("drops", 1)})
            else:
                self.registry.remove_token(symbol)
                self.market.remove_product(symbol)
                if int(entry.get("drops", 1)) >= self.MAX_READMIT_TRIES:
                    dropped.pop(symbol, None)
                    self.l.mark_candidate(entry.get("address", symbol), "dropped for good: " + verdict.reason(), now)
                else:
                    dropped[symbol] = {**entry, "dropped_at": now, "reason": verdict.reason(),
                                       "drops": int(entry.get("drops", 1)) + 1}
            changed = True
        if changed:
            self.l.put("dropped", dropped)
        return back

    def _total_supply(self, address):
        """The token's supply, as a string of wei, so the site can show market
        cap rather than a price with seven zeros. None when unreadable."""
        try:
            return str(int(self.client.total_supply(address)))
        except Exception:
            return None

    def prune(self, now, eth_usd):
        """Drop an unheld discovered token that no longer clears the screen."""
        dropped = []
        held = self.l.positions
        for symbol, entry in list(self.l.universe().items()):
            if not entry.get("total_supply") and entry.get("address"):
                supply = self._total_supply(entry["address"])
                if supply:
                    self.l.add_to_universe({**entry, "total_supply": supply})
            if entry.get("source") == "seed" or held.get(symbol, D(0)) > 0:
                continue
            verdict = self.screen.assess(symbol, entry["pool"], eth_usd, now)
            if not verdict.tradeable:
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
