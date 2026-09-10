"""Execution on Robinhood Chain, and the paper broker that mirrors it.

Live execution is a Uniswap v3 exactInputSingle with an on-chain minimum
output, sent from a local keystore wallet. Intent is persisted before the
request, and the signed transaction hash is recorded *before* the transaction
is broadcast, so an interrupted send is always recoverable by looking the hash
up on chain. An ambiguous result is never retried as a new swap.

Fill quantities are measured as balance deltas rather than taken from the
router's return value, so a memecoin that taxes transfers is accounted for at
what actually arrived.
"""

import os
import time

from .chain import ROUTER_ABI, checksum, hex32
from .config import D, from_wei, to_wei
from .fees import gross_fee_wei
from .risk import Veto

LIVE_OPT_IN = "I_ACCEPT_REAL_ONCHAIN_TRADES"


class UnresolvedOrder(RuntimeError):
    pass


class PaperBroker:
    """Simulated fills at the observed quote, with real fee accounting."""

    mode = "paper"

    def __init__(self, settings, ledger, wallets=None):
        self.s = settings
        self.l = ledger
        self.wallets = wallets or {}

    def preflight(self, eth_usd=None):
        from .fees import fee_wallet

        return {
            "mode": "paper",
            "network_execution": False,
            "network": self.s.network,
            "fly_wallet": self.wallets.get("trading"),
            "fee_wallet": fee_wallet(),
        }

    def verify_balances(self):
        pass

    def reconcile(self):
        # Paper requests never leave the process; the immutable plan carries the
        # execution quote, so an interrupted fill settles exactly once.
        for row in self.l.pending():
            self._fill(row["plan"])

    def execute(self, plan, before_submit):
        try:
            before_submit(plan)
        except Exception:
            self.l.mark(plan["client_order_id"], "REJECTED")
            raise
        return self._fill(plan)

    def _gas_wei(self):
        return int(D(self.s.paper_gas_price_gwei) * D(10**9)) * int(self.s.gas_limit)

    def _fill(self, p):
        amount_in = int(p["amount_in_wei"])
        qd = p["quote_decimals"]
        gas_wei = self._gas_wei()
        if p["side"] == "BUY":
            quote_wei = amount_in
            base_wei = to_wei(
                D(amount_in) / (D(10) ** qd) / D(p["observed_ask"]), p["base_decimals"]
            )
            fee_wei = int(p["planned_fee_wei"])
        else:
            base_wei = amount_in
            quote_wei = to_wei(
                D(amount_in) / (D(10) ** p["base_decimals"]) * D(p["observed_bid"]), qd
            )
            fee_wei = gross_fee_wei(quote_wei, p["fee_bps"])
        self.l.settle(
            p["client_order_id"], base_wei, quote_wei, fee_wei, gas_wei, time.time()
        )
        return {
            "mode": "paper",
            "status": "FILLED",
            "base_wei": str(base_wei),
            "quote_wei": str(quote_wei),
            "fee_wei": str(fee_wei),
            "gas_wei": str(gas_wei),
        }


class RobinhoodChainBroker:
    """Signed swaps against the registry's verified router."""

    mode = "live"

    def __init__(self, settings, ledger, client, registry, verified, account, fee_wallet):
        self.s = settings
        self.l = ledger
        self.client = client
        self.registry = registry
        self.verified = verified
        self.account = account
        self.address = checksum(account.address)
        self.fee_wallet = checksum(fee_wallet)
        self.router = client.contract(registry.router, ROUTER_ABI)
        self.quote_token = client.erc20(registry.quote_address)

    @classmethod
    def from_env(cls, settings, ledger, client, registry, verified):
        if os.environ.get("STONKFLYRH_LIVE") != LIVE_OPT_IN:
            raise RuntimeError("Live opt-in missing")
        from .fees import fee_wallet
        from .wallet import load

        destination = fee_wallet()
        if settings.protocol_fee_bps and not destination:
            raise RuntimeError(
                "This run charges a protocol fee but STONKFLYRH_FEE_WALLET is unset; "
                "the fee would have nowhere to go"
            )
        account = load("trading")
        return cls(
            settings, ledger, client, registry, verified, account, destination or account.address
        )

    # -- balances -------------------------------------------------------------

    def token_address(self, plan, role):
        """Resolve the plan's BASE/QUOTE leg to a concrete token address."""
        side = plan["token_in" if role == "in" else "token_out"]
        if side == "BASE":
            return self.registry.token(plan["product"])["address"]
        return self.registry.quote_address

    def balances(self, product):
        return {
            "gas": self.client.balance(self.address),
            "quote": int(self.quote_token.functions.balanceOf(self.address).call()),
            "base": int(
                self.client.erc20(self.registry.token(product)["address"])
                .functions.balanceOf(self.address)
                .call()
            ),
        }

    def unswept_fees(self):
        """Fees booked but not yet paid out still sit in the fly wallet."""
        return self.l.fees.outstanding()

    def expected(self):
        held = self.l.positions
        return {
            # USDG on hand is tradable cash plus fees awaiting a sweep.
            "quote": to_wei(self.l.cash, self.registry.quote_decimals) + self.unswept_fees(),
            "base": {
                p: to_wei(held.get(p, D(0)), self.registry.token(p)["decimals"])
                for p in self.s.products
            },
        }

    def verify_balances(self):
        """Reject external deposits and withdrawals rather than book them as P&L."""
        expected = self.expected()
        actual_quote = int(self.quote_token.functions.balanceOf(self.address).call())
        # A few units of tolerance absorb truncation in the wei conversions.
        if abs(actual_quote - expected["quote"]) > 4:
            raise RuntimeError(
                "USDG balance does not match the ledger; stop and reconcile rather "
                "than treat a transfer as profit"
            )
        for product, want in expected["base"].items():
            entry = self.registry.token(product)
            got = int(
                self.client.erc20(entry["address"])
                .functions.balanceOf(self.address)
                .call()
            )
            if abs(got - want) > 1:
                raise RuntimeError(f"{product} balance does not match the ledger")

    def preflight(self, eth_usd=None):
        self.reconcile()
        gas = self.client.balance(self.address)
        quote = int(self.quote_token.functions.balanceOf(self.address).call())
        if gas <= 0:
            raise RuntimeError("Trading wallet holds no ETH for gas")
        if not self.l.get("live_initialized"):
            if (
                self.l.get("tick")
                or self.l.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            ):
                raise RuntimeError("Uninitialized live ledger already has activity")
            for product in self.s.products:
                entry = self.registry.token(product)
                if int(
                    self.client.erc20(entry["address"])
                    .functions.balanceOf(self.address)
                    .call()
                ):
                    raise RuntimeError(
                        "Start from a wallet holding only USDG and gas ETH"
                    )
            qd = self.registry.quote_decimals
            cap = to_wei(self.s.capital_usd, qd)
            if not 0 < quote <= cap:
                raise RuntimeError(
                    f"Fund the fly wallet with 0 < USDG <= the configured "
                    f"${self.s.capital_usd} cap"
                )
            with self.l.transaction():
                held = from_wei(quote, qd)
                for k in ["cash", "initial_cash", "anchor"]:
                    self.l.put(k, str(held))
                self.l.put("live_initialized", True)
        self.verify_balances()
        return {
            "mode": "live",
            "network": self.client.net.name,
            "chain_id": self.client.net.chain_id,
            "fly_wallet": self.address,
            "fee_wallet": self.fee_wallet,
            "protocol_fee_bps": self.s.protocol_fee_bps,
            "router": self.registry.router,
            "gas_wei": str(gas),
        }

    # -- execution ------------------------------------------------------------

    def _gas_price(self):
        price = self.client.gas_price()
        ceiling = int(D(self.s.max_gas_price_gwei) * D(10**9))
        if price > ceiling:
            raise Veto(f"Gas price {price} wei above the configured ceiling")
        return price

    def _ensure_allowance(self, token, amount, gas_price):
        erc20 = self.client.erc20(token)
        current = int(
            erc20.functions.allowance(self.address, self.registry.router).call()
        )
        if current >= amount:
            return None
        # Approve exactly what this order needs. An unlimited approval would
        # leave a standing claim on the wallet after the run stops.
        tx = erc20.functions.approve(self.registry.router, int(amount)).build_transaction(
            self._tx_fields(gas_price, gas=120000)
        )
        return self._send(tx, "approve")

    def _tx_fields(self, gas_price, gas=None):
        return {
            "from": self.address,
            "chainId": self.client.net.chain_id,
            "nonce": self.client.nonce(self.address),
            "gas": int(gas or self.s.gas_limit),
            "gasPrice": int(gas_price),
        }

    def _send(self, tx, label, order_id=None):
        signed = self.account.sign_transaction(tx)
        tx_hash = hex32(signed.hash)
        if order_id:
            # Recorded before broadcast: the hash of a signed transaction is
            # fixed, so even a send that never returns can be looked up later.
            self.l.mark(order_id, "UNKNOWN", tx_hash)
        try:
            self.client.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as e:
            if order_id:
                raise UnresolvedOrder(
                    f"Broadcast outcome unknown for {tx_hash}; reconcile before trading again"
                ) from e
            raise RuntimeError(f"{label} transaction failed to broadcast") from e
        receipt = self._await(tx_hash)
        if receipt["status"] != 1:
            if order_id:
                self.l.mark(order_id, "REJECTED")
                return receipt
            raise RuntimeError(f"{label} transaction reverted")
        return receipt

    def _await(self, tx_hash, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                r = self.client.w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                r = None
            if r is not None:
                return {
                    "tx_hash": tx_hash,
                    "status": int(r["status"]),
                    "gas_used": int(r["gasUsed"]),
                    "effective_gas_price": int(
                        r.get("effectiveGasPrice") or self.client.gas_price()
                    ),
                    "block": int(r["blockNumber"]),
                }
            if time.monotonic() >= deadline:
                raise UnresolvedOrder(
                    f"Transaction {tx_hash} is not mined; execution stopped pending review"
                )
            time.sleep(2)

    def execute(self, p, before_submit):
        cid = p["client_order_id"]
        product = p["product"]
        token_in = self.token_address(p, "in")
        token_out = self.token_address(p, "out")
        amount_in = int(p["amount_in_wei"])
        min_out = int(p["min_out_wei"])
        approval_gas = 0
        try:
            gas_price = self._gas_price()
            self.verify_balances()
            before = self.balances(product)
            if before["gas"] < gas_price * self.s.gas_limit:
                raise Veto("Insufficient ETH for gas at the current price")
            approval = self._ensure_allowance(token_in, amount_in, gas_price)
            if approval:
                approval_gas = approval["gas_used"] * approval["effective_gas_price"]
            params = (
                checksum(token_in),
                checksum(token_out),
                int(p["pool_fee"]),
                self.address,
                amount_in,
                min_out,
                0,
            )
            call = self.router.functions.exactInputSingle(params)
            # Simulate against current state; a revert here never costs gas.
            simulated = int(call.call({"from": self.address}))
            if simulated < min_out:
                raise Veto("Simulated output below the slippage bound")
            if time.time() - p["quote_timestamp"] > self.s.max_quote_age:
                raise Veto("Quote expired during simulation")
            before_submit(p)
        except Exception:
            # An approval that already mined cost real gas even though the swap
            # never happened; equity should show it.
            self.l.charge_gas(approval_gas)
            self.l.mark(cid, "REJECTED")
            raise
        tx = call.build_transaction(self._tx_fields(gas_price))
        receipt = self._send(tx, "swap", order_id=cid)
        if receipt["status"] != 1:
            self.l.charge_gas(
                approval_gas + receipt["gas_used"] * receipt["effective_gas_price"]
            )
            return {"mode": "live", "status": "REVERTED", "tx_hash": receipt["tx_hash"]}
        return self._book(cid, p, receipt, before, approval_gas)

    def _book(self, cid, p, receipt, before, approval_gas=0):
        after = self.balances(p["product"])
        gas_wei = receipt["gas_used"] * receipt["effective_gas_price"] + approval_gas
        if p["side"] == "BUY":
            base_wei = after["base"] - before["base"]
            quote_wei = before["quote"] - after["quote"]
            fee_wei = int(p["planned_fee_wei"])
        else:
            base_wei = before["base"] - after["base"]
            quote_wei = after["quote"] - before["quote"]
            fee_wei = gross_fee_wei(quote_wei, p["fee_bps"])
        if base_wei <= 0 or quote_wei <= 0:
            raise UnresolvedOrder(
                "Swap mined but balances did not move as expected; reconcile manually"
            )
        self.l.settle(cid, base_wei, quote_wei, fee_wei, gas_wei, time.time())
        self.l.mark(cid, "SETTLED", receipt["tx_hash"])
        return {
            "mode": "live",
            "status": "SETTLED",
            "tx_hash": receipt["tx_hash"],
            "explorer": self.client.net.tx_url(receipt["tx_hash"]),
            "block": receipt["block"],
            "base_wei": str(base_wei),
            "quote_wei": str(quote_wei),
            "fee_wei": str(fee_wei),
            "gas_wei": str(gas_wei),
        }

    def reconcile(self):
        for row in self.l.pending():
            cid = row["id"]
            tx_hash = row["exchange_id"]
            if row["status"] == "PREPARED":
                # Nothing is broadcast before the UNKNOWN transition.
                self.l.mark(cid, "REJECTED")
                continue
            if not tx_hash:
                self.l.halt("Order in UNKNOWN state with no transaction hash")
                raise UnresolvedOrder(
                    "Unresolvable intent: inspect the trading wallet on the explorer. "
                    "No automatic resubmission."
                )
            try:
                r = self.client.w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                r = None
            if r is None:
                raise UnresolvedOrder(
                    f"Transaction {tx_hash} is still unmined; do not trade until it resolves"
                )
            if int(r["status"]) != 1:
                self.l.mark(cid, "REJECTED")
                continue
            self.l.halt(
                f"Swap {tx_hash} mined while this process was not watching; "
                "confirm balances against the ledger before resuming"
            )
            raise UnresolvedOrder(
                f"Mined but unbooked swap {tx_hash}; reconcile balances manually"
            )
