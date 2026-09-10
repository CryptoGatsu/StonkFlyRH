"""Protocol fee accrual, and paying it to the operator's own wallet.

Every fill can accrue a protocol fee denominated in the quote token (WETH, 18
decimals). The whole fee is owed to one beneficiary — the fee wallet — and is
swept there in batches rather than transferred per swap.

`protocol_fee_bps` defaults to 0. When the wallet paying the fee and the wallet
receiving it are both the operator's, a non-zero fee only moves their own money
and costs gas to do it; it exists for a run whose fees go somewhere else.
"""

import os

from .config import D

BPS_DENOMINATOR = 10000
BENEFICIARY = "treasury"

# Where swept fees go. STONKFLYRH_FEE_WALLET overrides it.
FEE_WALLET = "0x7f5afC67d4C3AE0182354ea6e785FdEb20150f15"


def fee_wallet():
    configured = os.environ.get("STONKFLYRH_FEE_WALLET") or FEE_WALLET
    if not configured:
        return None
    from .chain import checksum

    return checksum(configured)


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
            "fee_bps INTEGER NOT NULL)"
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
        """Book one fill's fee. Idempotent: re-settling cannot double-charge."""
        gross = gross_fee_wei(quote_wei, fee_bps)
        row = self.db.execute(
            "SELECT gross_wei FROM fees WHERE order_id=?", (order_id,)
        ).fetchone()
        if row:
            if row[0] != str(gross):
                raise RuntimeError("Fee accrual changed after it was booked")
            return {"gross_wei": gross}
        self.db.execute(
            "INSERT INTO fees VALUES (?,?,?,?,?)",
            (order_id, now, str(int(quote_wei)), str(gross), int(fee_bps)),
        )
        return {"gross_wei": gross}

    def accrued(self):
        row = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(CAST(gross_wei AS INTEGER)),0) FROM fees"
        ).fetchone()
        return {"fills_charged": int(row[0]), "gross_wei": int(row[1])}

    def paid(self):
        row = self.db.execute(
            "SELECT COALESCE(SUM(CAST(amount_wei AS INTEGER)),0) FROM fee_payouts "
            "WHERE status IN ('SENT','UNKNOWN')"
        ).fetchone()
        return int(row[0])

    def outstanding(self):
        return max(0, self.accrued()["gross_wei"] - self.paid())

    def record_payout(self, destination, amount_wei, now, status="PREPARED"):
        cur = self.db.execute(
            "INSERT INTO fee_payouts(created,beneficiary,destination,amount_wei,status,tx_hash) "
            "VALUES (?,?,?,?,?,NULL)",
            (now, BENEFICIARY, destination, str(int(amount_wei)), status),
        )
        return int(cur.lastrowid)

    def mark_payout(self, payout_id, status, tx_hash=None):
        self.db.execute(
            "UPDATE fee_payouts SET status=?,tx_hash=COALESCE(?,tx_hash) WHERE id=?",
            (status, tx_hash, payout_id),
        )

    def unresolved_payouts(self):
        rows = self.db.execute(
            "SELECT id,destination,amount_wei,status,tx_hash FROM fee_payouts "
            "WHERE status IN ('PREPARED','UNKNOWN') ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "destination": r[1],
                "amount_wei": int(r[2]),
                "status": r[3],
                "tx_hash": r[4],
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
            "fee_wallet": fee_wallet(),
            "gross": str(D(accrued["gross_wei"]) / scale),
            "paid_wei": self.paid(),
            "unpaid_wei": self.outstanding(),
        }
