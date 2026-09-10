"""Airdropping the operator's coin to wallets the fly judges worth it.

The fly already decides which tokens on Robinhood Chain are real: every pool
that clears the rug screen joins its universe. The wallets that buy those
tokens, and keep them, are the people this coin is for. That is the whole
judgement here: a wallet earns a drop by having bought tokens the screen
approved and by still holding at least one of them. Contracts, routers,
pools, the fly's own wallets and anyone already dropped are never recipients.

The coin leaves the deployer wallet, not the fly wallet: the fly wallet must
hold only USDG and gas ETH so its ledger stays reconcilable. Each transfer is
written to the ledger before it is signed and carries its hash before it is
broadcast, exactly like a donor payout, so an interrupted round is recoverable
and is never re-sent blindly. Amounts, recipients per round, a daily cap and a
reserve are settings; a round that would breach any of them sends less or
nothing.
"""


from .chain import checksum
from .config import from_wei, to_wei
from .donations import data_int, topic_address, transfer_topic

ZERO = "0x" + "00" * 20


def _pad(address):
    return "0x" + "00" * 12 + checksum(address)[2:].lower()


class Airdrop:
    CHUNK = 4000
    MAX_BLOCKS_PER_CENSUS = 60000

    def __init__(self, settings, client, registry, ledger, account, coin, deployer, excluded=()):
        self.s = settings
        self.client = client
        self.registry = registry
        self.l = ledger
        self.db = ledger.db
        self.account = account  # None in paper mode: rounds are dry runs
        self.coin = checksum(coin)
        self.deployer = checksum(deployer)
        self.token = client.erc20(self.coin)
        self.decimals = int(client.token_identity(self.coin)["decimals"])
        self.topic = transfer_topic()
        self.excluded = {checksum(a) for a in excluded if a} | {self.deployer, self.coin, ZERO}
        self.last_round = 0.0
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS airdrops (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "created REAL NOT NULL,address TEXT NOT NULL,amount_wei TEXT NOT NULL,"
            "reason TEXT NOT NULL,status TEXT NOT NULL,tx_hash TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS holders (address TEXT NOT NULL,token TEXT NOT NULL,"
            "symbol TEXT NOT NULL,first_block INTEGER NOT NULL,PRIMARY KEY (address,token))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS wallets (address TEXT PRIMARY KEY,"
            "is_contract INTEGER NOT NULL,checked_at REAL NOT NULL)"
        )

    # -- who has been buying what the screen approved ---------------------------

    def _sources(self, entry):
        """Where a token comes *from* when someone buys it."""
        out = {self.registry.router}
        if entry.get("venue", "v3") == "v3" and entry.get("pool"):
            out.add(entry["pool"])
        v4 = self.registry.v4 or {}
        for key in ("pool_manager", "universal_router"):
            if v4.get(key):
                out.add(v4[key])
        return [checksum(a) for a in out if a]

    def census(self, now, universe):
        """Record every wallet that bought a universe token since the last look."""
        head = int(self.client.w3.eth.block_number)
        start = self.l.get("airdrop_block")
        if start is None:
            start = max(0, head - int(self.s.airdrop_lookback_blocks))
        if head <= start:
            return {"from_block": start, "to_block": head, "buys": 0}
        hi = min(head, start + self.MAX_BLOCKS_PER_CENSUS)
        buys = 0
        for symbol, entry in universe.items():
            address = entry.get("address")
            if not address or checksum(address) == self.coin:
                continue
            sources = self._sources(entry)
            logs, hi_t = self.client.logs(
                {
                    "fromBlock": start + 1,
                    "toBlock": hi,
                    "address": checksum(address),
                    "topics": [self.topic, [_pad(s) for s in sources]],
                },
                chunk=self.CHUNK,
            )
            hi = min(hi, hi_t)
            for log in logs:
                buyer = topic_address(log["topics"][2])
                if buyer in self.excluded or buyer in sources or data_int(log["data"]) <= 0:
                    continue
                self.db.execute(
                    "INSERT OR IGNORE INTO holders VALUES (?,?,?,?)",
                    (buyer, checksum(address), symbol, int(log["blockNumber"])),
                )
                buys += 1
        with self.l.transaction():
            self.l.put("airdrop_block", hi)
            self.l.put("airdrop_backlog", head - hi)
        return {"from_block": start, "to_block": hi, "buys": buys, "backlog_blocks": head - hi}

    def _is_contract(self, address, now):
        row = self.db.execute("SELECT is_contract FROM wallets WHERE address=?", (address,)).fetchone()
        if row is not None:
            return bool(row[0])
        flag = bool(self.client.has_code(address))
        self.db.execute("INSERT OR REPLACE INTO wallets VALUES (?,?,?)", (address, int(flag), now))
        return flag

    def _still_holds(self, address, tokens):
        held = []
        for token, symbol in tokens:
            try:
                balance = int(self.client.erc20(token).functions.balanceOf(address).call())
            except Exception:
                continue
            if balance > 0:
                held.append(symbol)
        return held

    def dropped_already(self):
        return {
            r[0]
            for r in self.db.execute(
                "SELECT address FROM airdrops WHERE status IN ('PREPARED','UNKNOWN','SENT')"
            )
        }

    def candidates(self, now, limit):
        """The best wallets not yet dropped: most approved tokens bought, earliest
        first, still holding at least one, not a contract."""
        skip = self.excluded | self.dropped_already()
        rows = self.db.execute(
            "SELECT address, COUNT(DISTINCT token), MIN(first_block) FROM holders "
            "GROUP BY address ORDER BY 2 DESC, 3 ASC"
        ).fetchall()
        picks = []
        for address, distinct, first_block in rows:
            if len(picks) >= limit:
                break
            if address in skip or distinct < int(self.s.airdrop_min_tokens):
                continue
            if self._is_contract(address, now):
                continue
            tokens = self.db.execute(
                "SELECT token, symbol FROM holders WHERE address=?", (address,)
            ).fetchall()
            held = self._still_holds(address, tokens)
            if not held:
                continue
            picks.append(
                {
                    "address": address,
                    "bought": distinct,
                    "holds": sorted(held),
                    "since_block": first_block,
                    "reason": f"bought {distinct} screened token{'s' if distinct != 1 else ''}, "
                              f"still holds {', '.join(sorted(held))}",
                }
            )
        return picks

    # -- sending ------------------------------------------------------------------

    def due(self, now):
        return now - self.last_round >= self.s.airdrop_interval_seconds

    def sent_today_wei(self, now):
        row = self.db.execute(
            "SELECT COALESCE(SUM(CAST(amount_wei AS REAL)),0) FROM airdrops "
            "WHERE status IN ('UNKNOWN','SENT') AND created > ?",
            (now - 86400,),
        ).fetchone()
        return int(row[0] or 0)

    def unresolved(self):
        return self.db.execute(
            "SELECT id, tx_hash FROM airdrops WHERE status IN ('PREPARED','UNKNOWN')"
        ).fetchall()

    def reconcile(self):
        """Settle rows a previous process left mid-send before sending again."""
        for row_id, tx_hash in self.unresolved():
            if not tx_hash:
                self._mark(row_id, "REJECTED")
                continue
            try:
                r = self.client.w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                r = None
            if r is None:
                raise RuntimeError(
                    f"Airdrop {tx_hash} has no receipt yet; resolve it before another round"
                )
            self._mark(row_id, "SENT" if int(r["status"]) == 1 else "REJECTED")

    def _mark(self, row_id, status, tx_hash=None):
        if tx_hash is None:
            self.db.execute("UPDATE airdrops SET status=? WHERE id=?", (status, row_id))
        else:
            self.db.execute(
                "UPDATE airdrops SET status=?, tx_hash=? WHERE id=?", (status, tx_hash, row_id)
            )

    def budget(self, now):
        """How many drops this round may send, and why not more."""
        amount = to_wei(self.s.airdrop_amount, self.decimals)
        balance = int(self.token.functions.balanceOf(self.deployer).call())
        reserve = to_wei(self.s.airdrop_reserve, self.decimals)
        cap = to_wei(self.s.airdrop_daily_cap, self.decimals)
        spare = max(0, balance - reserve)
        left_today = max(0, cap - self.sent_today_wei(now))
        n = min(int(self.s.airdrop_recipients_per_round), spare // amount, left_today // amount)
        why = None
        if spare < amount:
            why = "deployer wallet holds less than one drop above the reserve"
        elif left_today < amount:
            why = "daily cap reached"
        return {
            "amount_wei": amount,
            "amount": str(from_wei(amount, self.decimals)),
            "balance": str(from_wei(balance, self.decimals)),
            "sendable": int(n),
            "why_not_more": why,
        }

    def round(self, now, universe, dry_run=None):
        """One round: census, pick, send. Dry when there is no key to sign with."""
        dry = dry_run if dry_run is not None else self.account is None
        self.last_round = now
        if not dry:
            self.reconcile()
        report = {"at": now, "dry_run": dry, "sent": [], "would_send": [], "skipped": None}
        report["census"] = self.census(now, universe)
        budget = self.budget(now)
        report["budget"] = {k: v for k, v in budget.items() if k != "amount_wei"}
        if budget["sendable"] <= 0:
            report["skipped"] = budget["why_not_more"]
            self.l.record_event("airdrops", report)
            return report
        if not dry and int(self.client.balance(self.deployer)) <= 0:
            report["skipped"] = "deployer wallet holds no ETH for gas"
            self.l.record_event("airdrops", report)
            return report
        picks = self.candidates(now, budget["sendable"])
        if not picks:
            report["skipped"] = "no wallet qualifies yet"
        for pick in picks:
            if dry:
                report["would_send"].append({**pick, "amount": budget["amount"]})
                continue
            result = self._send(pick, budget["amount_wei"], now)
            report["sent"].append({**pick, "amount": budget["amount"], **result})
        self.l.record_event("airdrops", report)
        return report

    def _send(self, pick, amount_wei, now):
        from .payouts import send_quote_token

        cur = self.db.execute(
            "INSERT INTO airdrops(created,address,amount_wei,reason,status) VALUES (?,?,?,?,?)",
            (now, pick["address"], str(int(amount_wei)), pick["reason"], "PREPARED"),
        )
        row_id = cur.lastrowid
        return send_quote_token(
            self.client,
            self.account,
            self.token,
            pick["address"],
            amount_wei,
            self.s,
            lambda status, tx_hash=None: self._mark(row_id, status, tx_hash),
        )

    # -- reporting ----------------------------------------------------------------

    def history(self, limit=50):
        rows = self.db.execute(
            "SELECT created,address,amount_wei,reason,status,tx_hash FROM airdrops "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "created": r[0],
                "address": r[1],
                "amount": str(from_wei(int(r[2]), self.decimals)),
                "reason": r[3],
                "status": r[4],
                "tx_hash": r[5],
            }
            for r in rows
        ]

    def report(self, now):
        holders = self.db.execute("SELECT COUNT(DISTINCT address) FROM holders").fetchone()[0]
        sent = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(CAST(amount_wei AS REAL)),0) FROM airdrops WHERE status='SENT'"
        ).fetchone()
        return {
            "coin": self.coin,
            "deployer": self.deployer,
            "dry_run": self.account is None,
            "amount": str(self.s.airdrop_amount),
            "recipients_per_round": int(self.s.airdrop_recipients_per_round),
            "interval_seconds": self.s.airdrop_interval_seconds,
            "daily_cap": str(self.s.airdrop_daily_cap),
            "wallets_seen": int(holders),
            "drops_sent": int(sent[0]),
            "total_sent": str(from_wei(int(sent[1]), self.decimals)),
            "scanned_to_block": self.l.get("airdrop_block"),
            "backlog_blocks": self.l.get("airdrop_backlog"),
            "next_round_in_seconds": max(0, int(self.last_round + self.s.airdrop_interval_seconds - now)),
        }
