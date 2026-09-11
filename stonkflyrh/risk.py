"""Execution limits can veto a neural proposal, never substitute a strategy.

Three things happen here that did not upstream, and all three only ever remove
options:

- **Dollar limits.** The quote asset is USDG, so a $10 order is 10 USDG with
  nothing to convert. The ETH/USD reference only values gas inside equity.
- **Adaptation.** Order size shrinks as realised volatility rises, and stops
  entirely past the configured ceiling. The cooldown stretches after a losing
  streak. Neither can grow a position past the configured cap.
- **The rug screen.** A buy the screen rejects does not happen. A sell is never
  screened and never blocked: whatever the screen thinks of a token, a run that
  holds it must always be able to get out.

One upstream check moved. The round-trip cost limit used to gate the whole
observation, which on a memecoin list means one token whose pool has been
drained freezes every trade in every other token. It now gates only the token
being traded, and for the exit from a token already recorded as a rug it is
relaxed to `rug_exit_spread`: a wide spread is the price of leaving a drained
pool, and holding to zero is worse. The on-chain minimum output still applies.
"""

import math
import time

from .config import D, from_wei, to_wei
from .fees import gross_fee_wei


class Veto(Exception):
    pass


def realised_volatility(history, window):
    """Standard deviation of per-observation log returns."""
    values = [float(v) for v in history[-(window + 1) :] if v and v > 0]
    if len(values) < 3:
        return None
    returns = [math.log(b / a) for a, b in zip(values, values[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return D(str(math.sqrt(variance)))


class Guard:
    def __init__(self, settings, ledger, stop_file, screen=None):
        self.s = settings
        self.l = ledger
        self.stop_file = stop_file
        self.screen = screen
        # Set by the run when activity tracking is on: the heat gate on buys.
        self.activity = None
        # Coins the operator has asked the run to leave (the `sell` command):
        # sold whole, through the exit spread, like a rug.
        self.exit_requests = set()

    # -- dollar limits ------------------------------------------------------

    def limits(self, eth_usd):
        """Limits in the quote asset. USDG is dollars, so these are the settings."""
        return {
            "eth_usd": D(eth_usd),
            "order_limit": D(self.s.order_limit_usd),
            "min_order": D(self.s.min_order_usd),
            "loss_stop": D(self.s.loss_stop_usd),
            "reward_deadband": D(self.s.reward_deadband_usd),
        }

    # -- adaptation ---------------------------------------------------------

    def size_scale(self, history):
        """Shrink toward the floor as volatility rises. Never above 1."""
        if not self.s.adapt_enabled or not history:
            return D(1), None
        vol = realised_volatility(history, self.s.volatility_window)
        if vol is None:
            return D(1), None
        calm = D(self.s.calm_volatility)
        wild = D(self.s.max_volatility)
        if vol <= calm:
            return D(1), vol
        if vol >= wild:
            raise Veto(f"realised volatility {vol * 100:.1f}% above the ceiling")
        floor = D(self.s.min_size_scale)
        span = (vol - calm) / (wild - calm)
        return D(1) - (D(1) - floor) * span, vol

    def exiting_a_rug(self, product, side):
        return side == "SELL" and (self.l.is_blocked(product) or product in self.exit_requests)

    def spread_limit(self, product, side):
        """The round-trip cost this trade may pay. Wider only to leave a rug."""
        if self.exiting_a_rug(product, side):
            return D(self.s.rug_exit_spread)
        return D(self.s.spread_limit)

    def move_tolerance(self, product, side):
        """How far the execution quote may sit from the one the fly saw."""
        if self.exiting_a_rug(product, side):
            return D(self.s.rug_exit_spread)
        return D(self.s.slippage)

    def cooldown_seconds(self):
        base = D(self.s.interval_seconds)
        if not self.s.adapt_enabled:
            return base
        streak = int(self.l.get("loss_streak") or 0)
        if streak < 2:
            return base
        return base * D(self.s.loss_cooldown_multiplier)

    # -- the guard ----------------------------------------------------------

    def check(self, quotes, now, eth_usd):
        limits = self.limits(eth_usd)
        if self.stop_file.exists():
            raise Veto("STOP file present")
        if self.l.get("halted"):
            raise Veto(self.l.get("halted"))
        if self.l.pending():
            raise Veto("Order outcome unresolved")
        if set(quotes) != set(self.l.products()):
            raise Veto("Incomplete market snapshot")
        for product, q in quotes.items():
            if q.product != product:
                raise Veto("Quote identity mismatch")
            if not -0.5 <= now - q.timestamp <= self.s.max_quote_age:
                raise Veto("Stale or future quote")
        contributed = D(self.l.get("initial_cash"))
        # An absolute stop for a small stake, a fractional one once the pool has
        # grown: whichever allows the larger drawdown, so donations do not make
        # a $25 stop trip on noise.
        stop = max(limits["loss_stop"], contributed * D(self.s.loss_stop_fraction))
        if self.l.equity(quotes, eth_usd) <= contributed - stop:
            self.l.halt("Loss stop reached; holdings remain exposed")
            raise Veto("Loss stop reached")

    def plan(
        self,
        product,
        side,
        quotes,
        eth_usd,
        now=None,
        gas_price_wei=None,
        history=None,
        pool=None,
    ):
        now = time.time() if now is None else now
        self.check(quotes, now, eth_usd)
        limits = self.limits(eth_usd)
        if product not in self.l.products() or side not in ("BUY", "SELL"):
            raise Veto("Invalid neural proposal")
        if now - self.l.get("last_attempt") < float(self.cooldown_seconds()):
            raise Veto("Order cooldown")
        if self.l.attempts_today(now) >= self.s.daily_orders:
            raise Veto("Daily order limit")
        q = quotes[product]
        qd = q.quote_decimals
        if q.round_trip > self.spread_limit(product, side):
            raise Veto("Round-trip cost above limit")
        if side == "SELL":
            # Volatility scales buys down and can stop them; it never stops an exit.
            scale, volatility = D(1), None
        else:
            scale, volatility = self.size_scale(history)
        order_limit = limits["order_limit"] * scale
        slip = D(1) - D(self.s.slippage)
        if side == "BUY":
            # Concentration: one order's worth per coin, a ceiling on how many
            # coins are held at once, and no re-entry into a coin just sold.
            held_now = self.l.positions.get(product, D(0))
            if held_now * q.bid + order_limit > D(self.s.max_position_usd):
                raise Veto(f"Already holding {product}: position cap ${D(self.s.max_position_usd)}")
            open_positions = self.l.open_positions()
            if product not in open_positions and len(open_positions) >= int(self.s.max_open_positions):
                raise Veto(f"Open positions at the cap of {int(self.s.max_open_positions)}")
            sold_at = self.l.last_sold_at(product) if hasattr(self.l, "last_sold_at") else None
            if sold_at is not None and now - float(sold_at) < float(self.s.reentry_cooldown_seconds):
                waited = (now - float(sold_at)) / 3600
                raise Veto(f"Sold {product} {waited:.1f}h ago; re-entry waits {float(self.s.reentry_cooldown_seconds) / 3600:.0f}h")
            # A buy is the only thing the screen can stop. It runs before any
            # sizing so a rejected token costs no further work.
            if self.screen is not None:
                self.screen.require(product, pool, eth_usd, now)
            elif self.l.is_blocked(product):
                raise Veto(f"{product} is blocklisted: {self.l.block_reason(product)}")
            if self.activity is not None and self.s.activity_enabled:
                # The screen's verdict can be a quarter of an hour old. A buy
                # asks the pool what happened in the last few minutes.
                ok, why = self.activity.buyable(product, self.l.universe().get(product) or {}, now,
                                                price=(q.bid + q.ask) / 2)
                if not ok:
                    raise Veto(f"{product} is not being traded right now: {why}")
            budget = min(order_limit, self.l.cash)
            notional_wei = to_wei(budget, qd)
            fee_wei = gross_fee_wei(notional_wei, self.s.protocol_fee_bps)
            amount_in_wei = notional_wei - fee_wei
            if amount_in_wei <= 0:
                raise Veto("Protocol fee consumes the whole budget")
            expected_out = from_wei(amount_in_wei, qd) / q.ask
            min_out_wei = to_wei(expected_out * slip, q.base_decimals)
            token_in, token_out = "QUOTE", "BASE"
        else:
            held = self.l.positions.get(product, D(0))
            # An exit is never scaled down by volatility and never capped by a
            # screen: getting out of a bad token is the one thing that must work.
            # Leaving a rug or a dead pool sells the whole position at once;
            # a slice would strand dust that can never clear the minimum.
            size = held if self.exiting_a_rug(product, side) else min(held, limits["order_limit"] / q.ask)
            amount_in_wei = to_wei(size, q.base_decimals)
            if amount_in_wei <= 0:
                raise Veto("No position to sell")
            expected_quote = from_wei(amount_in_wei, q.base_decimals) * q.bid
            notional_wei = to_wei(expected_quote, qd)
            fee_wei = gross_fee_wei(notional_wei, self.s.protocol_fee_bps)
            min_out_wei = to_wei(expected_quote * slip, qd)
            token_in, token_out = "BASE", "QUOTE"
        if min_out_wei <= 0:
            raise Veto("Slippage bound rounds the minimum output to zero")
        if notional_wei < to_wei(limits["min_order"], qd):
            raise Veto("Order notional below the configured minimum")
        gas_cost_wei = 0
        if gas_price_wei is not None:
            from .pricing import gas_to_usd

            gas_cost_wei = int(gas_price_wei) * int(self.s.gas_limit)
            gas_usd = gas_to_usd(gas_cost_wei, eth_usd)
            if gas_usd > from_wei(notional_wei, qd) * D(self.s.max_gas_share):
                raise Veto("Gas cost too large a share of the order")
        return {
            "product": product,
            "side": side,
            "token_in": token_in,
            "token_out": token_out,
            "amount_in_wei": str(amount_in_wei),
            "min_out_wei": str(min_out_wei),
            "notional_wei": str(notional_wei),
            "notional_usd": str(from_wei(notional_wei, qd)),
            "planned_fee_wei": str(fee_wei),
            "fee_bps": self.s.protocol_fee_bps,
            "fee_basis": "input" if side == "BUY" else "output",
            "base_decimals": q.base_decimals,
            "quote_decimals": qd,
            "pool_fee": q.pool_fee,
            "eth_usd": str(D(eth_usd)),
            "size_scale": str(scale),
            "volatility": str(volatility) if volatility is not None else None,
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
