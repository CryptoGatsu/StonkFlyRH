"""Paying out accrued protocol fees, 20% of them to the development wallet.

Fees are booked on every fill and swept in batches rather than transferred per
trade: a sweep is one ERC-20 transfer per beneficiary instead of two per swap,
which on a busy run is the difference between a few cents of gas and a lot of
it. The split itself is fixed at accrual time and is not recomputed here.

A payout row is written before the transfer is signed and carries the signed
transaction hash before broadcast, so an interrupted sweep is recoverable and
is never re-sent blindly.
"""

import time

from .chain import checksum, hex32
from .config import D, QUOTE_DECIMALS, from_wei, to_wei
from .fees import dev_wallet

BENEFICIARIES = ("development", "treasury")


class FeeSweeper:
    def __init__(self, settings, ledger, client, registry, account, fee_wallet):
        self.s = settings
        self.l = ledger
        self.fees = ledger.fees
        self.client = client
        self.registry = registry
        self.account = account
        self.address = checksum(account.address)
        self.fee_wallet = checksum(fee_wallet)
        self.token = client.erc20(registry.quote_address)

    def destination(self, beneficiary):
        if beneficiary == "development":
            target = dev_wallet()
            if not target:
                raise RuntimeError(
                    "STONKFLYRH_DEV_WALLET is not set; the development share has "
                    "nowhere to go"
                )
            return target
        return self.fee_wallet

    def outstanding(self):
        return {b: self.fees.outstanding(b) for b in BENEFICIARIES}

    def reconcile(self):
        """Resolve any sweep that was interrupted before it was confirmed."""
        for row in self.fees.unresolved_payouts():
            if row["status"] == "PREPARED":
                # Nothing is broadcast before the hash is recorded.
                self.fees.mark_payout(row["id"], "REJECTED")
                continue
            tx_hash = self.l.db.execute(
                "SELECT tx_hash FROM fee_payouts WHERE id=?", (row["id"],)
            ).fetchone()[0]
            if not tx_hash:
                raise RuntimeError(
                    "Fee payout in UNKNOWN state with no transaction hash; "
                    "inspect the fee wallet before sweeping again"
                )
            try:
                r = self.client.w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                r = None
            if r is None:
                raise RuntimeError(
                    f"Fee payout {tx_hash} is still unmined; do not sweep again yet"
                )
            self.fees.mark_payout(row["id"], "SENT" if int(r["status"]) == 1 else "REJECTED")

    def sweep(self, minimum=None, dry_run=False):
        self.reconcile()
        floor = to_wei(minimum if minimum is not None else self.s.min_order_quote, QUOTE_DECIMALS)
        held = int(self.token.functions.balanceOf(self.address).call())
        results = []
        for beneficiary in BENEFICIARIES:
            owed = self.fees.outstanding(beneficiary)
            row = {
                "beneficiary": beneficiary,
                "destination": self.destination(beneficiary),
                "outstanding_wei": owed,
                "outstanding": str(from_wei(owed, QUOTE_DECIMALS)),
            }
            if owed < floor:
                results.append({**row, "status": "BELOW_THRESHOLD"})
                continue
            if owed > held:
                results.append({**row, "status": "INSUFFICIENT_BALANCE"})
                continue
            if dry_run:
                results.append({**row, "status": "WOULD_SEND"})
                continue
            results.append({**row, **self._transfer(beneficiary, row["destination"], owed)})
            held -= owed
        return {
            "wallet": self.address,
            "dev_share_percent": 20.0,
            "payouts": results,
        }

    def _transfer(self, beneficiary, destination, amount_wei):
        payout_id = self.fees.record_payout(
            beneficiary, destination, amount_wei, time.time()
        )
        gas_price = self.client.gas_price()
        ceiling = int(D(self.s.max_gas_price_gwei) * D(10**9))
        if gas_price > ceiling:
            self.fees.mark_payout(payout_id, "REJECTED")
            return {"status": "GAS_ABOVE_CEILING"}
        tx = self.token.functions.transfer(destination, int(amount_wei)).build_transaction(
            {
                "from": self.address,
                "chainId": self.client.net.chain_id,
                "nonce": self.client.nonce(self.address),
                "gas": 120000,
                "gasPrice": gas_price,
            }
        )
        signed = self.account.sign_transaction(tx)
        tx_hash = hex32(signed.hash)
        self.fees.mark_payout(payout_id, "UNKNOWN", tx_hash)
        try:
            self.client.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as e:
            raise RuntimeError(
                f"Fee payout {tx_hash} broadcast outcome unknown; reconcile before sweeping"
            ) from e
        deadline = time.monotonic() + 180
        while True:
            try:
                r = self.client.w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                r = None
            if r is not None:
                ok = int(r["status"]) == 1
                self.fees.mark_payout(payout_id, "SENT" if ok else "REJECTED")
                return {
                    "status": "SENT" if ok else "REVERTED",
                    "tx_hash": tx_hash,
                    "explorer": self.client.net.tx_url(tx_hash),
                }
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Fee payout {tx_hash} not mined; resolve it before sweeping again"
                )
            time.sleep(2)
