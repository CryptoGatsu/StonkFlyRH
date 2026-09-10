"""Recognising donations, and paying donors their share.

Anyone can send USDG to the fly wallet. The bot notices by reading the USDG
contract's Transfer events to that address and keeping every one whose
transaction is not one of its own — swaps and payouts it signed itself are
excluded by hash, so sell proceeds are never mistaken for a gift. Each
recognised transfer is booked as a deposit into the pool at that moment's NAV
and into the ledger as cash, so the balance check that halts the run on an
unexplained balance change sees it as explained.

Payouts run on a schedule. Each donor whose stake has set a new high-water mark
is settled: their share of the gain is sent as USDG, the rest becomes the
operator's units. A payout row is written before the transfer is signed, and
the signed hash is recorded before broadcast, so an interrupted sweep is
recoverable and never re-sent. Money leaves only when cash covers it after the
next order is still affordable.

Nothing here can place a trade or change what the fly trades.
"""


from .chain import checksum
from .config import D, from_wei, to_wei

TRANSFER = "Transfer(address,address,uint256)"


def transfer_topic():
    from eth_utils import keccak

    return "0x" + keccak(text=TRANSFER).hex()


def topic_address(topic):
    raw = bytes(topic) if not isinstance(topic, str) else bytes.fromhex(topic[2:])
    return checksum("0x" + raw[-20:].hex())


def data_int(data):
    raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
    return int.from_bytes(raw[-32:], "big")


def hex_of(value):
    if isinstance(value, str):
        return value if value.startswith("0x") else "0x" + value
    from .chain import hex32

    return hex32(value)


class Donations:
    CHUNK = 4000

    def __init__(self, settings, ledger, pool, client, registry, account, wallet):
        self.s = settings
        self.l = ledger
        self.pool = pool
        self.client = client
        self.registry = registry
        self.account = account
        self.wallet = checksum(wallet)
        self.token = client.erc20(registry.quote_address)
        self.qd = int(registry.quote_decimals)
        self.topic = transfer_topic()
        self.last_payout = 0.0

    # -- recognising deposits -----------------------------------------------

    def start_at_head(self, now):
        """A fresh run counts only what arrives from now on: whatever funded the
        wallet before this is the operator's stake, not a donation."""
        if self.l.get("donations_block") is None:
            self.l.put("donations_block", int(self.client.w3.eth.block_number))

    def own_hashes(self):
        hashes = set(self.pool.own_tx_hashes()) | set(self.l.fees_tx_hashes())
        for row in self.l.db.execute(
            "SELECT exchange_id FROM orders WHERE exchange_id IS NOT NULL"
        ):
            hashes.add(row[0])
        return {h.lower() for h in hashes if h}

    def ingest(self, now, eth_usd, quotes):
        """Book every inbound USDG transfer that is not one of our own."""
        head = int(self.client.w3.eth.block_number)
        start = self.l.get("donations_block")
        if start is None:
            self.l.put("donations_block", head)
            return []
        if head <= start:
            return []
        padded = "0x" + "00" * 12 + self.wallet[2:].lower()
        logs, head = self.client.logs(
            {
                "fromBlock": start + 1,
                "toBlock": head,
                "address": self.registry.quote_address,
                "topics": [self.topic, None, padded],
            },
            chunk=self.CHUNK,
            max_blocks=60000,
        )
        own = self.own_hashes()
        booked = []
        for log in logs:
            tx_hash = hex_of(log["transactionHash"]).lower()
            if tx_hash in own:
                continue
            sender = topic_address(log["topics"][1])
            amount_wei = data_int(log["data"])
            if amount_wei <= 0:
                continue
            amount = from_wei(amount_wei, self.qd)
            index = int(log.get("logIndex", 0))
            # Equity without this money: it is already in the wallet balance the
            # caller measured, so the pool prices units off the pre-deposit value.
            equity_before = self.l.equity(quotes, eth_usd) if quotes else self.l.cash
            record = self.l.deposit(amount, now)
            if record is None:
                continue
            address = sender if amount >= D(self.s.donation_min_usd) else "operator"
            entry = self.pool.deposit(
                address, amount, equity_before, now, tx_hash, index, int(log["blockNumber"])
            )
            if entry is not None:
                booked.append(
                    {
                        "address": sender,
                        "amount": str(amount),
                        "nav": str(entry["nav"]),
                        "units": str(entry["units"]),
                        "tx_hash": tx_hash,
                        "credited_to": address,
                    }
                )
        self.l.put("donations_block", head)
        if booked:
            self.l.record_event("donations", {"at": now, "deposits": booked})
        return booked

    # -- paying out -----------------------------------------------------------

    def due(self, now):
        return now - self.last_payout >= self.s.donor_payout_interval_seconds

    def reconcile(self):
        for row in self.pool.unresolved_payouts():
            if row["status"] == "PREPARED":
                self.pool.mark_payout(row["id"], "REJECTED")
                continue
            if not row["tx_hash"]:
                raise RuntimeError("Donor payout in UNKNOWN state with no hash; inspect the wallet")
            try:
                r = self.client.w3.eth.get_transaction_receipt(row["tx_hash"])
            except Exception:
                r = None
            if r is None:
                raise RuntimeError(f"Donor payout {row['tx_hash']} still unmined; not paying again yet")
            self.pool.mark_payout(row["id"], "SENT" if int(r["status"]) == 1 else "REJECTED")

    def pay(self, now, eth_usd, quotes, dry_run=False):
        self.last_payout = now
        self.reconcile()
        equity = self.l.equity(quotes, eth_usd)
        owed = self.pool.due(equity, now, self.s.donor_min_payout_usd)
        results = []
        for item in owed:
            # Keep enough cash for the next order after paying, or wait.
            spare = self.l.cash - D(self.s.order_limit_usd)
            if item["payout"] > spare:
                results.append({**self._row(item), "status": "DEFERRED_CASH"})
                continue
            if dry_run:
                results.append({**self._row(item), "status": "WOULD_SEND"})
                continue
            settled = self.pool.settle(item["address"], equity, now)
            if settled is None:
                continue
            amount_wei = to_wei(settled["payout"], self.qd)
            outcome = self._transfer(item["address"], amount_wei, settled["gain"], now)
            if outcome["status"] in ("SENT", "UNKNOWN"):
                self.l.withdraw(settled["payout"], now)
            results.append({**self._row(item), **outcome})
        if results:
            self.l.record_event("donor_payouts", {"at": now, "payouts": results})
        return results

    def _row(self, item):
        return {
            "address": item["address"],
            "gain": str(item["gain"].quantize(D("0.01"))),
            "payout": str(item["payout"].quantize(D("0.01"))),
            "nav": str(item["nav"]),
        }

    def _transfer(self, destination, amount_wei, gain, now):
        from .payouts import send_quote_token

        payout_id = self.pool.record_payout(destination, amount_wei, str(gain), now)
        return send_quote_token(
            self.client, self.account, self.token, checksum(destination), amount_wei,
            self.s, lambda status, tx=None: self.pool.mark_payout(payout_id, status, tx),
        )


class FixtureDonations:
    """Offline stand-in: one synthetic donor arrives on the second tick."""

    def __init__(self, settings, ledger, pool):
        self.s = settings
        self.l = ledger
        self.pool = pool
        self.ticks = 0
        self.last_payout = 0.0

    def start_at_head(self, now):
        pass

    def ingest(self, now, eth_usd, quotes):
        self.ticks += 1
        if self.ticks != 2:
            return []
        equity_before = self.l.equity(quotes, eth_usd) if quotes else self.l.cash
        self.l.deposit(D("25"), now)
        entry = self.pool.deposit("0x" + "d0" * 20, D("25"), equity_before, now, "0xfixture", 0, 0)
        booked = [{"address": "0x" + "d0" * 20, "amount": "25", "nav": str(entry["nav"]),
                   "units": str(entry["units"]), "tx_hash": "0xfixture", "credited_to": "0x" + "d0" * 20}]
        self.l.record_event("donations", {"at": now, "deposits": booked})
        return booked

    def due(self, now):
        return now - self.last_payout >= self.s.donor_payout_interval_seconds

    def pay(self, now, eth_usd, quotes, dry_run=False):
        self.last_payout = now
        equity = self.l.equity(quotes, eth_usd)
        results = []
        for item in self.pool.due(equity, now, self.s.donor_min_payout_usd):
            settled = self.pool.settle(item["address"], equity, now)
            if settled:
                self.l.withdraw(settled["payout"], now)
                results.append({"address": item["address"], "payout": str(settled["payout"]),
                                "gain": str(settled["gain"]), "status": "SIMULATED"})
        if results:
            self.l.record_event("donor_payouts", {"at": now, "payouts": results})
        return results
