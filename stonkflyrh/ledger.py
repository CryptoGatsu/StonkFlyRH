"""Durable money accounting and order intent. All values use Decimal strings.

Cash is USDG, which is dollars; positions are memecoin quantities; gas is
native ETH. Gas is tracked separately from tradable cash and subtracted from
equity at the current ETH/USD reference, so the reinforcement signal reflects
what the run actually costs to operate.
"""

import contextlib
import dataclasses
import json
import sqlite3
import uuid
from pathlib import Path

from .config import D, GAS_DECIMALS, from_wei
from .pricing import gas_to_usd
from .fees import FeeBook


class Ledger:
    def __init__(self, path, settings, mode, capital=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY,status TEXT NOT NULL,"
            "created REAL NOT NULL,plan TEXT NOT NULL,exchange_id TEXT,settlement TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS screens (product TEXT PRIMARY KEY,"
            "checked REAL NOT NULL,verdict TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS rugs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "product TEXT NOT NULL,at REAL NOT NULL,record TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS blocklist (product TEXT PRIMARY KEY,"
            "at REAL NOT NULL,reason TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS candidates (address TEXT PRIMARY KEY,"
            "at REAL NOT NULL,outcome TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS discovery (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "at REAL NOT NULL,report TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "kind TEXT NOT NULL,at REAL NOT NULL,payload TEXT NOT NULL)"
        )
        self.fees = FeeBook(self)
        if self.get("settings") is None:
            if capital is None:
                raise ValueError("A new ledger needs its starting USDG balance")
            with self.transaction():
                for k, v in {
                    "settings": settings.signature(),
                    # Kept so `fees` and other later commands rebuild the exact
                    # run settings instead of guessing at the defaults.
                    "settings_full": dataclasses.asdict(settings),
                    "mode": mode,
                    "network": settings.network,
                    "cash": str(capital),
                    "initial_cash": str(capital),
                    "positions": {},
                    "entries": {},
                    "universe": {
                        p: {"symbol": p, "name": p, "source": "seed", "added_at": 0}
                        for p in settings.products
                    },
                    "gas_spent": "0",
                    "anchor": str(capital),
                    "tick": 0,
                    "checkpoint": None,
                    "halted": None,
                    "last_attempt": 0,
                }.items():
                    self.put(k, v)
        elif self.get("mode") != mode:
            raise RuntimeError("Run mode mismatch; use a separate run directory")
        elif self.get("settings") != settings.signature():
            self._migrate_settings(settings)

    # Values that define what the run *is*. Changing one of these under a live
    # ledger would silently re-denominate cash or re-price donors, so they still
    # refuse. Everything else (screen thresholds, discovery pacing, order size,
    # adaptation) is a tuning knob an operator is expected to turn between
    # restarts; the change is recorded against the ledger instead of halting.
    FROZEN_SETTINGS = ("network", "quote_symbol", "quote_decimals", "donor_share")

    def _migrate_settings(self, settings):
        """Accept a settings change on an existing run: added fields (an
        upgrade) and tuned values are recorded; a change to a frozen value is
        refused."""
        stored = dict(self.get("settings_full") or {})
        current = dataclasses.asdict(settings)
        changed = {
            k: (stored[k], current[k])
            for k in stored
            if k in current and k != "products" and stored[k] != current[k]
        }
        frozen = {k: v for k, v in changed.items() if k in self.FROZEN_SETTINGS}
        if frozen:
            raise RuntimeError(
                "Run settings changed: "
                + ", ".join(f"{k} {a!r} -> {b!r}" for k, (a, b) in frozen.items())
                + ". Use a separate run directory, or restore the old values."
            )
        added = sorted(k for k in current if k not in stored)
        with self.transaction():
            self.put("settings", settings.signature())
            self.put("settings_full", current)
            self.db.execute(
                "INSERT INTO events(kind,at,payload) VALUES (?,?,?)",
                (
                    "migration",
                    0,
                    json.dumps(
                        {
                            "settings_added": added,
                            "settings_changed": {
                                k: [a, b] for k, (a, b) in sorted(changed.items())
                            },
                        },
                        allow_nan=False,
                    ),
                ),
            )

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (key, json.dumps(value, allow_nan=False)),
        )

    @property
    def cash(self):
        return D(self.get("cash"))

    @property
    def gas_spent(self):
        return D(self.get("gas_spent") or 0)

    @property
    def positions(self):
        return {k: D(v) for k, v in self.get("positions").items()}

    def equity(self, quotes, eth_usd):
        """Dollars: cash plus holdings at bid, less gas at the current ETH price."""
        held = sum((v * quotes[p].bid for p, v in self.positions.items()), D(0))
        gas_wei = int(self.gas_spent * (D(10) ** GAS_DECIMALS))
        return self.cash + held - gas_to_usd(gas_wei, eth_usd)

    def halt(self, reason):
        self.put("halted", reason)

    def reserve(self, plan, now):
        cid = str(uuid.uuid4())
        plan = {**plan, "client_order_id": cid}
        with self.transaction():
            if self.pending():
                raise RuntimeError("Unreconciled order exists")
            self.db.execute(
                "INSERT INTO orders(id,status,created,plan) VALUES (?,?,?,?)",
                (cid, "PREPARED", now, json.dumps(plan)),
            )
            self.put("last_attempt", now)
        return plan

    def mark(self, cid, status, exchange_id=None):
        self.db.execute(
            "UPDATE orders SET status=?,exchange_id=COALESCE(?,exchange_id) WHERE id=?",
            (status, exchange_id, cid),
        )

    def pending(self):
        rows = self.db.execute(
            "SELECT id,status,created,plan,exchange_id FROM orders "
            "WHERE status NOT IN ('SETTLED','REJECTED') ORDER BY created"
        ).fetchall()
        return [
            {
                "id": r[0],
                "status": r[1],
                "created": r[2],
                "plan": json.loads(r[3]),
                "exchange_id": r[4],
            }
            for r in rows
        ]

    def attempts_today(self, now):
        return self.db.execute(
            "SELECT COUNT(*) FROM orders WHERE created>=?", (now - now % 86400,)
        ).fetchone()[0]

    def order(self, cid):
        row = self.db.execute(
            "SELECT id,status,created,plan,exchange_id,settlement FROM orders WHERE id=?",
            (cid,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "status": row[1],
            "created": row[2],
            "plan": json.loads(row[3]),
            "exchange_id": row[4],
            "settlement": json.loads(row[5]) if row[5] else None,
        }

    def settle(self, cid, base_wei, quote_wei, fee_wei, gas_wei=0, now=0):
        """Book one fill in integer base units, then accrue its protocol fee.

        Settlement and fee accrual share one transaction: a run can never show a
        filled trade whose fee, and therefore whose 20% development share, was
        not booked with it.
        """
        base_wei, quote_wei, fee_wei, gas_wei = (
            int(base_wei),
            int(quote_wei),
            int(fee_wei),
            int(gas_wei),
        )
        if min(base_wei, quote_wei, fee_wei, gas_wei) < 0:
            raise ValueError("Negative settlement")
        if (base_wei == 0 and (quote_wei or fee_wei)) or (base_wei > 0 and quote_wei == 0):
            raise ValueError("Inconsistent fill quantities")
        with self.transaction():
            row = self.db.execute(
                "SELECT status,plan,settlement FROM orders WHERE id=?", (cid,)
            ).fetchone()
            if not row:
                raise RuntimeError("Unknown order")
            payload = {
                "base_wei": str(base_wei),
                "quote_wei": str(quote_wei),
                "fee_wei": str(fee_wei),
                "gas_wei": str(gas_wei),
            }
            if row[0] == "SETTLED":
                if json.loads(row[2]) != payload:
                    raise RuntimeError("Settlement changed after finalization")
                return
            if row[0] == "REJECTED":
                raise RuntimeError("Cannot settle a rejected intent")
            p = json.loads(row[1])
            base = from_wei(base_wei, p["base_decimals"])
            quote = from_wei(quote_wei, p["quote_decimals"])
            fee = from_wei(fee_wei, p["quote_decimals"])
            positions = self.positions
            held = positions.get(p["product"], D(0))
            cash = self.cash
            if p["side"] == "BUY":
                if quote_wei > int(p["amount_in_wei"]):
                    raise RuntimeError("Buy spent more than the reserved input")
                if base_wei < int(p["min_out_wei"]):
                    raise RuntimeError("Buy filled below the slippage bound")
                if fee_wei != int(p["planned_fee_wei"]):
                    raise RuntimeError("Buy-side fee differs from the reserved fee")
                cash -= quote + fee
                positions[p["product"]] = held + base
            else:
                if base_wei > int(p["amount_in_wei"]):
                    raise RuntimeError("Sell exceeded the reserved quantity")
                if quote_wei < int(p["min_out_wei"]):
                    raise RuntimeError("Sell filled below the slippage bound")
                cash += quote - fee
                positions[p["product"]] = held - base
            if cash < 0 or positions[p["product"]] < 0:
                raise RuntimeError("Fill exceeds reserved account funds")
            self.put("cash", str(cash))
            self.put("positions", {k: str(v) for k, v in positions.items()})
            self.put("gas_spent", str(self.gas_spent + from_wei(gas_wei, GAS_DECIMALS)))
            self.db.execute(
                "UPDATE orders SET status='SETTLED',settlement=? WHERE id=?",
                (json.dumps(payload), cid),
            )
            basis = quote_wei if p["fee_basis"] == "output" else int(p["notional_wei"])
            booked = self.fees.accrue(cid, basis, p["fee_bps"], now)
            if booked["gross_wei"] != fee_wei:
                raise RuntimeError("Charged fee does not match the booked accrual")
            if p["side"] == "SELL" and positions[p["product"]] == 0:
                # Position closed; the rug reference for it is no longer live.
                entries = dict(self.get("entries") or {})
                entries.pop(p["product"], None)
                self.put("entries", entries)

    # -- the universe --------------------------------------------------------

    def universe(self):
        return dict(self.get("universe") or {})

    def products(self):
        return list(self.universe())

    def add_to_universe(self, entry):
        universe = self.universe()
        universe[entry["symbol"]] = dict(entry)
        self.put("universe", universe)

    def remove_from_universe(self, symbol, reason, now):
        universe = self.universe()
        if symbol in universe:
            removed = universe.pop(symbol)
            self.put("universe", universe)
            self.record_discovery(
                {"at": now, "dropped": [{"symbol": symbol, "reason": reason, **removed}]}
            )
            # Remembered with everything needed to screen it again later: a
            # token that fails today may clear tomorrow.
            dropped = dict(self.get("dropped") or {})
            previous = dropped.get(symbol) or {}
            dropped[symbol] = {**removed, "reason": reason, "dropped_at": now,
                               "drops": int(previous.get("drops", 0)) + 1}
            self.put("dropped", dropped)

    def dropped(self):
        return dict(self.get("dropped") or {})

    def seed_universe(self, registry, verified, now):
        """Fill seed entries with what the registry and the chain know."""
        universe = self.universe()
        changed = False
        for symbol in registry.tokens:
            entry = registry.tokens[symbol]
            pool = (verified or {}).get("pools", {}).get(symbol, {})
            if symbol not in universe or not universe[symbol].get("address"):
                universe[symbol] = {
                    "symbol": symbol,
                    "name": entry.get("name", symbol),
                    "address": entry["address"],
                    "decimals": entry["decimals"],
                    "pool_fee": pool.get("fee", entry.get("pool_fee")),
                    "pool": pool.get("pool"),
                    "source": universe.get(symbol, {}).get("source", "seed"),
                    "added_at": universe.get(symbol, {}).get("added_at", now),
                }
                changed = True
        if changed:
            self.put("universe", universe)
        return universe

    def mark_candidate(self, address, outcome, now):
        self.db.execute(
            "INSERT OR REPLACE INTO candidates VALUES (?,?,?)", (address, now, outcome)
        )

    def seen_candidates(self):
        return [r[0] for r in self.db.execute("SELECT address FROM candidates")]

    def record_discovery(self, report):
        self.db.execute(
            "INSERT INTO discovery(at,report) VALUES (?,?)",
            (report.get("at", 0), json.dumps(report, allow_nan=False)),
        )

    def discoveries(self, limit=20):
        rows = self.db.execute(
            "SELECT report FROM discovery ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # -- money arriving and leaving outside of trades --------------------------

    def deposit(self, amount, now):
        """Book an external deposit: cash, contributed capital and the reward
        anchor all rise together, so new money is never read as profit."""
        amount = D(amount)
        if amount <= 0:
            return None
        with self.transaction():
            self.put("cash", str(self.cash + amount))
            self.put("initial_cash", str(D(self.get("initial_cash")) + amount))
            self.put("anchor", str(D(self.get("anchor")) + amount))
            self.put("deposited_total", str(D(self.get("deposited_total") or 0) + amount))
        return {"amount": str(amount), "at": now}

    def withdraw(self, amount, now):
        """Book money leaving as a payout: cash and the anchor fall together."""
        amount = D(amount)
        if amount <= 0:
            return None
        if amount > self.cash:
            raise RuntimeError("Withdrawal exceeds cash")
        with self.transaction():
            self.put("cash", str(self.cash - amount))
            self.put("anchor", str(D(self.get("anchor")) - amount))
            self.put("withdrawn_total", str(D(self.get("withdrawn_total") or 0) + amount))
        return {"amount": str(amount), "at": now}

    def last_marks(self):
        """Bid-like marks from the last observation, for valuing positions when
        no fresh quotes exist yet (a deposit noticed at preflight)."""
        observation = self.get("observation") or {}
        history = observation.get("market_history") or {}

        class Mark:
            def __init__(self, bid):
                self.bid = bid

        return {p: Mark(D(str(h[-1]))) for p, h in history.items() if h}

    def record_event(self, kind, payload):
        self.db.execute(
            "INSERT INTO events(kind,at,payload) VALUES (?,?,?)",
            (kind, payload.get("at", 0), json.dumps(payload, allow_nan=False)),
        )

    def events(self, kind, limit=20):
        rows = self.db.execute(
            "SELECT payload FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def fees_tx_hashes(self):
        return {
            r[0]
            for r in self.db.execute(
                "SELECT tx_hash FROM fee_payouts WHERE tx_hash IS NOT NULL"
            )
        }

    # -- rug memory ----------------------------------------------------------

    def screen_put(self, product, verdict, now):
        self.db.execute(
            "INSERT OR REPLACE INTO screens VALUES (?,?,?)",
            (product, now, json.dumps(verdict, allow_nan=False)),
        )

    def screen_raw(self, product):
        row = self.db.execute(
            "SELECT verdict FROM screens WHERE product=?", (product,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def screen_get(self, product, now, ttl):
        row = self.db.execute(
            "SELECT checked,verdict FROM screens WHERE product=?", (product,)
        ).fetchone()
        if not row or now - row[0] > ttl:
            return None
        return json.loads(row[1])

    def record_rug(self, record):
        """A rug is permanent: the token is blocked for the life of the run."""
        with self.transaction():
            self.db.execute(
                "INSERT INTO rugs(product,at,record) VALUES (?,?,?)",
                (record["product"], record["at"], json.dumps(record, allow_nan=False)),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO blocklist VALUES (?,?,?)",
                (record["product"], record["at"], record["reason"]),
            )

    def rugs(self):
        rows = self.db.execute(
            "SELECT record FROM rugs ORDER BY id"
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def block(self, product, reason, now):
        self.db.execute(
            "INSERT OR REPLACE INTO blocklist VALUES (?,?,?)", (product, now, reason)
        )

    def is_blocked(self, product):
        return (
            self.db.execute(
                "SELECT 1 FROM blocklist WHERE product=?", (product,)
            ).fetchone()
            is not None
        )

    def block_reason(self, product):
        row = self.db.execute(
            "SELECT reason FROM blocklist WHERE product=?", (product,)
        ).fetchone()
        return row[0] if row else None

    def blocklist(self):
        rows = self.db.execute(
            "SELECT product,at,reason FROM blocklist ORDER BY at"
        ).fetchall()
        return [{"product": r[0], "at": r[1], "reason": r[2]} for r in rows]

    def charge_gas(self, gas_wei):
        """Book gas spent outside a settlement, such as an ERC-20 approval."""
        gas_wei = int(gas_wei)
        if gas_wei < 0:
            raise ValueError("Negative gas")
        if not gas_wei:
            return
        with self.transaction():
            self.put("gas_spent", str(self.gas_spent + from_wei(gas_wei, GAS_DECIMALS)))

    def commit_tick(self, anchor, checkpoint, observation=None):
        with self.transaction():
            self.put("anchor", str(anchor))
            self.put("checkpoint", checkpoint)
            self.put("tick", self.get("tick") + 1)
            if observation is not None:
                self.put("observation", observation)

    def close(self):
        self.db.close()
