"""Paying accrued protocol fees out to the fee wallet.

Fees are booked on every fill and swept in batches rather than transferred per
trade: one ERC-20 transfer instead of one per swap, which on a busy run is the
difference between a few cents of gas and a lot of it.

A payout row is written before the transfer is signed and carries the signed
transaction hash before broadcast, so an interrupted sweep is recoverable and
is never re-sent blindly.
"""

import time

from .chain import checksum, hex32
from .config import D, from_wei, to_wei
from .fees import BENEFICIARY, fee_wallet


def send_quote_token(client, account, token, destination, amount_wei, settings, mark):
    """Sign, record the hash, broadcast, wait. `mark(status, tx_hash)` persists
    each transition so an interrupted send is recoverable and never repeated."""
    gas_price = client.gas_price()
    ceiling = int(D(settings.max_gas_price_gwei) * D(10**9))
    if gas_price > ceiling:
        mark("REJECTED")
        return {"status": "GAS_ABOVE_CEILING"}
    tx = token.functions.transfer(destination, int(amount_wei)).build_transaction(
        {
            "from": checksum(account.address),
            "chainId": client.net.chain_id,
            "nonce": client.nonce(account.address),
            "gas": 120000,
            "gasPrice": gas_price,
        }
    )
    signed = account.sign_transaction(tx)
    tx_hash = hex32(signed.hash)
    mark("UNKNOWN", tx_hash)
    try:
        client.w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as e:
        raise RuntimeError(
            f"Transfer {tx_hash} broadcast outcome unknown; reconcile before sending again"
        ) from e
    deadline = time.monotonic() + 180
    while True:
        try:
            r = client.w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            r = None
        if r is not None:
            ok = int(r["status"]) == 1
            mark("SENT" if ok else "REJECTED")
            return {
                "status": "SENT" if ok else "REVERTED",
                "tx_hash": tx_hash,
                "explorer": client.net.tx_url(tx_hash),
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Transfer {tx_hash} not mined; resolve it before sending again")
        time.sleep(2)


class FeeSweeper:
    def __init__(self, settings, ledger, client, registry, account, destination=None):
        self.s = settings
        self.l = ledger
        self.fees = ledger.fees
        self.client = client
        self.registry = registry
        self.account = account
        self.address = checksum(account.address)
        self.configured = checksum(destination) if destination else None
        self.token = client.erc20(registry.quote_address)

    def destination(self):
        target = self.configured or fee_wallet()
        if not target:
            raise RuntimeError(
                "STONKFLYRH_FEE_WALLET is not set; accrued fees have nowhere to go"
            )
        if checksum(target) == self.address:
            raise RuntimeError(
                "The fee wallet is the fly wallet; a sweep would only pay gas to "
                "move money to itself"
            )
        return checksum(target)

    def outstanding(self):
        return self.fees.outstanding()

    def reconcile(self):
        """Resolve any sweep that was interrupted before it was confirmed."""
        for row in self.fees.unresolved_payouts():
            if row["status"] == "PREPARED":
                # Nothing is broadcast before the hash is recorded.
                self.fees.mark_payout(row["id"], "REJECTED")
                continue
            tx_hash = row["tx_hash"]
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
        qd = self.registry.quote_decimals
        floor = to_wei(minimum if minimum is not None else "1", qd)
        held = int(self.token.functions.balanceOf(self.address).call())
        owed = self.fees.outstanding()
        row = {
            "beneficiary": BENEFICIARY,
            "destination": self.destination(),
            "outstanding_wei": owed,
            "outstanding": str(from_wei(owed, qd)),
        }
        if owed < floor:
            row["status"] = "BELOW_THRESHOLD"
        elif owed > held:
            row["status"] = "INSUFFICIENT_BALANCE"
        elif dry_run:
            row["status"] = "WOULD_SEND"
        else:
            row.update(self._transfer(row["destination"], owed))
        return {"wallet": self.address, "payouts": [row]}

    def _transfer(self, destination, amount_wei):
        payout_id = self.fees.record_payout(destination, amount_wei, time.time())
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
