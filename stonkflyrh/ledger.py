"""Durable money accounting and order intent. All values use Decimal strings.

Balances are WETH; positions are memecoin quantities; gas is native ETH. Gas is
tracked separately from tradable cash but subtracted from equity, so the
reinforcement signal reflects what the run actually costs to operate.
"""

import contextlib
import dataclasses
import json
import sqlite3
import uuid
from pathlib import Path

from .config import D, QUOTE_DECIMALS, from_wei
from .fees import FeeBook


class Ledger:
    def __init__(self, path, settings, mode, capital_weth=None):
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
        self.fees = FeeBook(self)
        if self.get("settings") is None:
            if capital_weth is None:
                raise ValueError("A new ledger needs its starting WETH balance")
            with self.transaction():
                for k, v in {
                    "settings": settings.signature(),
                    # Kept so `fees` and other later commands rebuild the exact
                    # run settings instead of guessing at the defaults.
                    "settings_full": dataclasses.asdict(settings),
                    "mode": mode,
                    "network": settings.network,
                    "cash": str(capital_weth),
                    "initial_cash": str(capital_weth),
                    "positions": {},
                    "entries": {},
                    "gas_spent": "0",
                    "anchor": str(capital_weth),
                    "tick": 0,
                    "checkpoint": None,
                    "halted": None,
                    "last_attempt": 0,
                }.items():
                    self.put(k, v)
        elif self.get("settings") != settings.signature() or self.get("mode") != mode:
            raise RuntimeError(
                "Run settings/mode mismatch; use a separate run directory"
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

    def equity(self, quotes):
        held = sum((v * quotes[p].bid for p, v in self.positions.items()), D(0))
        return self.cash + held - self.gas_spent

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
            quote = from_wei(quote_wei, QUOTE_DECIMALS)
            fee = from_wei(fee_wei, QUOTE_DECIMALS)
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
            self.put("gas_spent", str(self.gas_spent + from_wei(gas_wei, 18)))
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
            self.put("gas_spent", str(self.gas_spent + from_wei(gas_wei, 18)))

    def commit_tick(self, anchor, checkpoint, observation=None):
        with self.transaction():
            self.put("anchor", str(anchor))
            self.put("checkpoint", checkpoint)
            self.put("tick", self.get("tick") + 1)
            if observation is not None:
                self.put("observation", observation)

    def close(self):
        self.db.close()
