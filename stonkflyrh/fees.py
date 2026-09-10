"""Protocol fee accrual and the fixed 20% development split.

Every fill accrues a protocol fee denominated in the quote token (WETH, 18
decimals). The fee is split at the moment it is booked:

    development share  = 20% of the gross fee, owed to DEV_WALLET
    treasury share     = the remainder, owed to the run's own fee wallet

The split is done in integer wei with the remainder assigned to the treasury,
so the two shares always add back to the gross exactly. There is no rounding
path that can pay out more than was collected.

Gas is not a protocol fee and is never included here: gas leaves the trading
wallet as ETH and is recorded separately by the broker.
"""

import os

from .config import D

# 20% of protocol fees are earmarked for development. This is a constant of the
# fork, not a tunable setting, so it cannot drift run to run.
DEV_SHARE_BPS = 2000
BPS_DENOMINATOR = 10000

# Development wallet for the 20% share. Paste the address between the quotes to
# bake it in as this fork's default; STONKFLYRH_DEV_WALLET overrides it either
# way. Left empty, the split is still computed and booked on every fill, live
# mode refuses to start, and a sweep refuses to send: the share always has a
# recorded owner even before it has a destination.
DEV_WALLET = ""


def dev_wallet():
    configured = os.environ.get("STONKFLYRH_DEV_WALLET") or DEV_WALLET
    if not configured:
        return None
    from .chain import checksum

    return checksum(configured)


def split_fee(gross_wei):
    """Exact integer split. dev + treasury == gross for every input."""
    gross = int(gross_wei)
    if gross < 0:
        raise ValueError("Negative fee")
    dev = gross * DEV_SHARE_BPS // BPS_DENOMINATOR
    treasury = gross - dev
    if dev + treasury != gross or dev < 0 or treasury < 0:
        raise RuntimeError("Fee split did not conserve the gross fee")
    return dev, treasury


def gross_fee_wei(quote_wei, fee_bps):
    """Protocol fee charged on the quote-token value of a fill, rounded down."""
    quote_wei = int(quote_wei)
    fee_bps = int(fee_bps)
    if quote_wei < 0 or not 0 <= fee_bps <= BPS_DENOMINATOR:
        raise ValueError("Invalid fee basis")
    return quote_wei * fee_bps // BPS_DENOMINATOR


class FeeBook:
    """Durable, append-only record of every accrued fee and every payout."""

    def __init__(self, ledger):
        self.l = ledger
        self.db = ledger.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fees ("
            "order_id TEXT PRIMARY KEY,"
            "created REAL NOT NULL,"
            "quote_wei TEXT NOT NULL,"
            "gross_wei TEXT NOT NULL,"
            "dev_wei TEXT NOT NULL,"
            "treasury_wei TEXT NOT NULL,"
            "dev_share_bps INTEGER NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fee_payouts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "created REAL NOT NULL,"
            "beneficiary TEXT NOT NULL,"
            "destination TEXT NOT NULL,"
            "amount_wei TEXT NOT NULL,"
            "status TEXT NOT NULL,"
            "tx_hash TEXT)"
        )

    def accrue(self, order_id, quote_wei, fee_bps, now):
        """Book one fill's fee. Idempotent: re-settling an order cannot double-charge."""
        gross = gross_fee_wei(quote_wei, fee_bps)
        dev, treasury = split_fee(gross)
        row = self.db.execute(
            "SELECT gross_wei,dev_wei,treasury_wei FROM fees WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if row:
            if (row[0], row[1], row[2]) != (str(gross), str(dev), str(treasury)):
                raise RuntimeError("Fee accrual changed after it was booked")
            return {"gross_wei": gross, "dev_wei": dev, "treasury_wei": treasury}
        self.db.execute(
            "INSERT INTO fees VALUES (?,?,?,?,?,?,?)",
            (
                order_id,
                now,
                str(int(quote_wei)),
                str(gross),
                str(dev),
                str(treasury),
                DEV_SHARE_BPS,
            ),
        )
        return {"gross_wei": gross, "dev_wei": dev, "treasury_wei": treasury}

    def accrued(self):
        row = self.db.execute(
            "SELECT COUNT(*),"
            "COALESCE(SUM(CAST(gross_wei AS INTEGER)),0),"
            "COALESCE(SUM(CAST(dev_wei AS INTEGER)),0),"
            "COALESCE(SUM(CAST(treasury_wei AS INTEGER)),0) FROM fees"
        ).fetchone()
        return {
            "fills_charged": int(row[0]),
            "gross_wei": int(row[1]),
            "dev_wei": int(row[2]),
            "treasury_wei": int(row[3]),
        }

    def paid(self, beneficiary):
        row = self.db.execute(
            "SELECT COALESCE(SUM(CAST(amount_wei AS INTEGER)),0) FROM fee_payouts "
            "WHERE beneficiary=? AND status IN ('SENT','UNKNOWN')",
            (beneficiary,),
        ).fetchone()
        return int(row[0])

    def outstanding(self, beneficiary):
        accrued = self.accrued()
        owed = accrued["dev_wei"] if beneficiary == "development" else accrued["treasury_wei"]
        return max(0, owed - self.paid(beneficiary))

    def record_payout(self, beneficiary, destination, amount_wei, now, status="PREPARED"):
        cur = self.db.execute(
            "INSERT INTO fee_payouts(created,beneficiary,destination,amount_wei,status,tx_hash) "
            "VALUES (?,?,?,?,?,NULL)",
            (now, beneficiary, destination, str(int(amount_wei)), status),
        )
        return int(cur.lastrowid)

    def mark_payout(self, payout_id, status, tx_hash=None):
        self.db.execute(
            "UPDATE fee_payouts SET status=?,tx_hash=COALESCE(?,tx_hash) WHERE id=?",
            (status, tx_hash, payout_id),
        )

    def unresolved_payouts(self):
        rows = self.db.execute(
            "SELECT id,beneficiary,destination,amount_wei,status FROM fee_payouts "
            "WHERE status IN ('PREPARED','UNKNOWN') ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "beneficiary": r[1],
                "destination": r[2],
                "amount_wei": int(r[3]),
                "status": r[4],
            }
            for r in rows
        ]

    def payouts(self, limit=50):
        rows = self.db.execute(
            "SELECT created,beneficiary,destination,amount_wei,status,tx_hash "
            "FROM fee_payouts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "created": r[0],
                "beneficiary": r[1],
                "destination": r[2],
                "amount_wei": int(r[3]),
                "status": r[4],
                "tx_hash": r[5],
            }
            for r in rows
        ]

    def report(self, quote_decimals=18):
        accrued = self.accrued()
        scale = D(10) ** quote_decimals
        return {
            **accrued,
            "dev_share_bps": DEV_SHARE_BPS,
            "dev_share_percent": DEV_SHARE_BPS / 100,
            "dev_wallet": dev_wallet(),
            "gross": str(D(accrued["gross_wei"]) / scale),
            "development": str(D(accrued["dev_wei"]) / scale),
            "treasury": str(D(accrued["treasury_wei"]) / scale),
            "development_unpaid_wei": self.outstanding("development"),
            "treasury_unpaid_wei": self.outstanding("treasury"),
        }
