"""Execution limits can veto a neural proposal, never substitute a strategy."""

import time

from .config import D, QUOTE_DECIMALS, from_wei, to_wei
from .fees import gross_fee_wei


class Veto(Exception):
    pass


class Guard:
    def __init__(self, settings, ledger, stop_file):
        self.s = settings
        self.l = ledger
        self.stop_file = stop_file

    def check(self, quotes, now):
        if self.stop_file.exists():
            raise Veto("STOP file present")
        if self.l.get("halted"):
            raise Veto(self.l.get("halted"))
        if self.l.pending():
            raise Veto("Order outcome unresolved")
        if set(quotes) != set(self.s.products):
            raise Veto("Incomplete market snapshot")
        for product, q in quotes.items():
            if q.product != product:
                raise Veto("Quote identity mismatch")
            if not -0.5 <= now - q.timestamp <= self.s.max_quote_age:
                raise Veto("Stale or future quote")
            if q.round_trip > D(self.s.spread_limit):
                raise Veto("Round-trip cost above limit")
        if self.l.equity(quotes) <= D(self.l.get("initial_cash")) - D(self.s.loss_stop):
            self.l.halt("Loss stop reached; holdings remain exposed")
            raise Veto("Loss stop reached")

    def plan(self, product, side, quotes, now=None, gas_price_wei=None):
        now = time.time() if now is None else now
        self.check(quotes, now)
        if product not in self.s.products or side not in ("BUY", "SELL"):
            raise Veto("Invalid neural proposal")
        if now - self.l.get("last_attempt") < self.s.interval_seconds:
            raise Veto("Order cooldown")
        if self.l.attempts_today(now) >= self.s.daily_orders:
            raise Veto("Daily order limit")
        q = quotes[product]
        slip = D(1) - D(self.s.slippage)
        if side == "BUY":
            budget = min(D(self.s.order_limit), self.l.cash)
            notional_wei = to_wei(budget, QUOTE_DECIMALS)
            fee_wei = gross_fee_wei(notional_wei, self.s.protocol_fee_bps)
            amount_in_wei = notional_wei - fee_wei
            if amount_in_wei <= 0:
                raise Veto("Protocol fee consumes the whole budget")
            expected_out = from_wei(amount_in_wei, QUOTE_DECIMALS) / q.ask
            min_out_wei = to_wei(expected_out * slip, q.base_decimals)
            token_in, token_out = "QUOTE", "BASE"
        else:
            held = self.l.positions.get(product, D(0))
            size = min(held, D(self.s.order_limit) / q.ask)
            amount_in_wei = to_wei(size, q.base_decimals)
            if amount_in_wei <= 0:
                raise Veto("No position to sell")
            expected_quote = from_wei(amount_in_wei, q.base_decimals) * q.bid
            notional_wei = to_wei(expected_quote, QUOTE_DECIMALS)
            fee_wei = gross_fee_wei(notional_wei, self.s.protocol_fee_bps)
            min_out_wei = to_wei(expected_quote * slip, QUOTE_DECIMALS)
            token_in, token_out = "BASE", "QUOTE"
        if min_out_wei <= 0:
            raise Veto("Slippage bound rounds the minimum output to zero")
        if notional_wei < to_wei(self.s.min_order_quote, QUOTE_DECIMALS):
            raise Veto("Order notional below the configured minimum")
        gas_cost_wei = 0
        if gas_price_wei is not None:
            gas_cost_wei = int(gas_price_wei) * int(self.s.gas_limit)
            if D(gas_cost_wei) > D(notional_wei) * D(self.s.max_gas_share):
                raise Veto("Gas cost too large a share of the order")
        return {
            "product": product,
            "side": side,
            "token_in": token_in,
            "token_out": token_out,
            "amount_in_wei": str(amount_in_wei),
            "min_out_wei": str(min_out_wei),
            "notional_wei": str(notional_wei),
            "planned_fee_wei": str(fee_wei),
            "fee_bps": self.s.protocol_fee_bps,
            "fee_basis": "input" if side == "BUY" else "output",
            "base_decimals": q.base_decimals,
            "pool_fee": q.pool_fee,
            "gas_limit": self.s.gas_limit,
            "gas_cost_estimate_wei": str(gas_cost_wei),
            "observed_bid": str(q.bid),
            "observed_ask": str(q.ask),
            "quote_timestamp": q.timestamp,
            "settings": self.s.signature(),
            "order_type": "uniswap_v3_exact_input_single",
        }

    def before_submit(self, plan):
        # Called after the on-chain simulation and balance checks, at the final
        # send boundary.
        if self.stop_file.exists() or self.l.get("halted"):
            raise Veto("Execution stopped")
        if not -0.5 <= time.time() - plan["quote_timestamp"] <= self.s.max_quote_age:
            raise Veto("Quote expired before submission")
        pending = self.l.pending()
        if (
            len(pending) != 1
            or pending[0]["id"] != plan["client_order_id"]
            or pending[0]["status"] != "PREPARED"
        ):
            raise Veto("Intent ownership mismatch")
