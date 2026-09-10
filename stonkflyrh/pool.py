"""Who owns what in the fly's wallet, and what each is owed.

Once anyone but the operator can add money, "profit" needs a definition that
survives deposits arriving at different times and prices. This is the standard
one: the wallet is a pool, ownership is units, and the value of a unit (NAV) is
equity divided by units outstanding. A deposit buys units at the NAV of that
moment. Nothing about a deposit changes anyone else's value.

Each participant carries a high-water mark: the NAV per unit at which they were
last settled. When NAV rises above it, the gain on their units is split — the
donor's share is paid out in USDG, the rest goes to the operator — and both
halves are taken by moving units, so everyone else's NAV is untouched. After a
settlement the participant holds exactly their high-water value again: gains
are paid, principal rides on, and losses are theirs until a new high is set.

All quantities are Decimal; units are scaled so 1 unit = 1 USDG at the start.
"""


import uuid

from .config import D

OPERATOR = "operator"
UNIT_STEP = D("0.000001")


def q(value):
    return D(value).quantize(UNIT_STEP)


class Pool:
    def __init__(self, ledger, donor_share="0.5"):
        self.l = ledger
        self.db = ledger.db
        self.donor_share = D(donor_share)
        if not D(0) <= self.donor_share <= D(1):
            raise ValueError("Donor share must be a fraction")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS participants (address TEXT PRIMARY KEY,"
            "units TEXT NOT NULL,hwm TEXT NOT NULL,deposited TEXT NOT NULL,"
            "paid_out TEXT NOT NULL,first_seen REAL NOT NULL,last_settled REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS deposits (tx_hash TEXT NOT NULL,log_index INTEGER NOT NULL,"
            "address TEXT NOT NULL,amount TEXT NOT NULL,nav TEXT NOT NULL,units TEXT NOT NULL,"
            "at REAL NOT NULL,block INTEGER,PRIMARY KEY (tx_hash,log_index))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS donor_payouts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "created REAL NOT NULL,address TEXT NOT NULL,amount_wei TEXT NOT NULL,"
            "gain TEXT NOT NULL,status TEXT NOT NULL,tx_hash TEXT)"
        )

    # -- state ----------------------------------------------------------------

    def participants(self):
        rows = self.db.execute(
            "SELECT address,units,hwm,deposited,paid_out,first_seen,last_settled "
            "FROM participants ORDER BY first_seen"
        ).fetchall()
        return [
            {
                "address": r[0],
                "units": D(r[1]),
                "hwm": D(r[2]),
                "deposited": D(r[3]),
                "paid_out": D(r[4]),
                "first_seen": r[5],
                "last_settled": r[6],
            }
            for r in rows
        ]

    def participant(self, address):
        for p in self.participants():
            if p["address"] == address:
                return p
        return None

    def units_total(self):
        row = self.db.execute(
            "SELECT COALESCE(SUM(CAST(units AS REAL)),0) FROM participants"
        ).fetchone()
        # SQLite sums as float; recompute exactly from the rows.
        return sum((p["units"] for p in self.participants()), D(0)) if row else D(0)

    def nav(self, equity):
        units = self.units_total()
        if units <= 0:
            return D(1)
        return D(equity) / units

    def _write(self, p):
        self.db.execute(
            "INSERT OR REPLACE INTO participants VALUES (?,?,?,?,?,?,?)",
            (
                p["address"],
                str(p["units"]),
                str(p["hwm"]),
                str(p["deposited"]),
                str(p["paid_out"]),
                p["first_seen"],
                p["last_settled"],
            ),
        )

    # -- deposits -------------------------------------------------------------

    def seed_operator(self, capital, now):
        """The operator's own stake: the first units, at 1 USDG each."""
        if self.participant(OPERATOR) is None and D(capital) > 0:
            self._write(
                {
                    "address": OPERATOR,
                    "units": D(capital),
                    "hwm": D(1),
                    "deposited": D(capital),
                    "paid_out": D(0),
                    "first_seen": now,
                    "last_settled": now,
                }
            )

    def deposit(self, address, amount, equity_before, now, tx_hash="", log_index=0, block=None):
        """Book an inbound amount as units bought at the current NAV.

        `equity_before` is equity without this deposit: the money is already in
        the wallet when it is noticed, so the caller subtracts it first. A
        second call with the same tx_hash/log_index is a no-op.
        """
        amount = D(amount)
        if amount <= 0:
            raise ValueError("Deposit must be positive")
        if tx_hash and self.db.execute(
            "SELECT 1 FROM deposits WHERE tx_hash=? AND log_index=?", (tx_hash, log_index)
        ).fetchone():
            return None
        if not tx_hash:
            # A deposit with no on-chain hash (fixtures, the operator's stake)
            # still gets a unique row.
            tx_hash = "local:" + uuid.uuid4().hex
        nav = self.nav(equity_before)
        units = amount / nav
        p = self.participant(address) or {
            "address": address,
            "units": D(0),
            "hwm": nav,
            "deposited": D(0),
            "paid_out": D(0),
            "first_seen": now,
            "last_settled": now,
        }
        # A deposit at a NAV above the participant's mark lifts the mark for the
        # blended position, so new money is never charged for old gains.
        if p["units"] > 0:
            p["hwm"] = (p["units"] * p["hwm"] + units * nav) / (p["units"] + units)
        else:
            p["hwm"] = nav
        p["units"] += units
        p["deposited"] += amount
        self._write(p)
        self.db.execute(
            "INSERT INTO deposits VALUES (?,?,?,?,?,?,?,?)",
            (tx_hash, log_index, address, str(amount), str(nav), str(q(units)), now, block),
        )
        return {"address": address, "amount": amount, "nav": nav, "units": units}

    # -- settlement -----------------------------------------------------------

    def due(self, equity, now, minimum):
        """What each donor is owed at this NAV, without changing anything."""
        nav = self.nav(equity)
        owed = []
        for p in self.participants():
            if p["address"] == OPERATOR or p["units"] <= 0 or nav <= p["hwm"]:
                continue
            gain = (nav - p["hwm"]) * p["units"]
            payout = gain * self.donor_share
            if payout < D(minimum):
                continue
            owed.append({"address": p["address"], "gain": gain, "payout": payout, "nav": nav})
        return owed

    def settle(self, address, equity, now):
        """Crest a participant: pay their share, move the rest to the operator.

        Returns the payout amount (USDG) or None. Units leave the donor equal to
        the whole gain at this NAV, so their remaining value is their previous
        high-water value; the operator's share arrives as units, so the pool's
        NAV is unchanged by the transfer itself.
        """
        nav = self.nav(equity)
        p = self.participant(address)
        if p is None or p["address"] == OPERATOR or nav <= p["hwm"]:
            return None
        gain = (nav - p["hwm"]) * p["units"]
        payout = gain * self.donor_share
        fee = gain - payout
        p["units"] -= gain / nav
        p["hwm"] = nav
        p["paid_out"] += payout
        p["last_settled"] = now
        op = self.participant(OPERATOR)
        if op is None:
            self.seed_operator(D(0), now)
            op = {"address": OPERATOR, "units": D(0), "hwm": nav, "deposited": D(0),
                  "paid_out": D(0), "first_seen": now, "last_settled": now}
        op["units"] += fee / nav
        with self.l.transaction():
            self._write(p)
            self._write(op)
        return {"address": address, "gain": gain, "payout": payout, "fee": fee, "nav": nav}

    # -- payouts (money leaving) ----------------------------------------------

    def record_payout(self, address, amount_wei, gain, now):
        cur = self.db.execute(
            "INSERT INTO donor_payouts(created,address,amount_wei,gain,status,tx_hash) "
            "VALUES (?,?,?,?,?,NULL)",
            (now, address, str(int(amount_wei)), str(gain), "PREPARED"),
        )
        return int(cur.lastrowid)

    def mark_payout(self, payout_id, status, tx_hash=None):
        self.db.execute(
            "UPDATE donor_payouts SET status=?,tx_hash=COALESCE(?,tx_hash) WHERE id=?",
            (status, tx_hash, payout_id),
        )

    def unresolved_payouts(self):
        rows = self.db.execute(
            "SELECT id,address,amount_wei,status,tx_hash FROM donor_payouts "
            "WHERE status IN ('PREPARED','UNKNOWN') ORDER BY id"
        ).fetchall()
        return [
            {"id": r[0], "address": r[1], "amount_wei": int(r[2]), "status": r[3], "tx_hash": r[4]}
            for r in rows
        ]

    def payouts(self, limit=50):
        rows = self.db.execute(
            "SELECT created,address,amount_wei,gain,status,tx_hash FROM donor_payouts "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"created": r[0], "address": r[1], "amount_wei": int(r[2]), "gain": r[3],
             "status": r[4], "tx_hash": r[5]}
            for r in rows
        ]

    def own_tx_hashes(self):
        return {
            r[0]
            for r in self.db.execute(
                "SELECT tx_hash FROM donor_payouts WHERE tx_hash IS NOT NULL"
            )
        }

    # -- reporting ------------------------------------------------------------

    def report(self, equity):
        nav = self.nav(equity)
        rows = []
        for p in self.participants():
            value = p["units"] * nav
            rows.append(
                {
                    "address": p["address"],
                    "deposited": str(p["deposited"]),
                    "units": str(q(p["units"])),
                    "value": str(value.quantize(D("0.01"))),
                    "paid_out": str(p["paid_out"].quantize(D("0.01"))),
                    "hwm": str(p["hwm"]),
                    "unrealised": str(((nav - p["hwm"]) * p["units"]).quantize(D("0.01"))),
                    "first_seen": p["first_seen"],
                }
            )
        return {
            "nav": str(nav),
            "units": str(q(self.units_total())),
            "donor_share": str(self.donor_share),
            "participants": rows,
            "donors": len([p for p in rows if p["address"] != OPERATOR]),
        }
