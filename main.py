"""Surge-rider bot for make.fun's "Race a BTC trading bot live for 24 hours".

Strategy (long-only, one asset CBBTC, 1-minute bars, $1,000 paper USDC):

  * Entry: buy with the full cash balance when the close has surged at least
    65 bps over the last three bars (a short, strong momentum burst). A round
    trip costs about 26 bps in fees, slippage and price impact, so the entry
    only fires on a move comfortably larger than that.
  * Exit: sell the whole position when the close falls 50 bps below the
    highest close seen since entry (a trailing stop measured on closes,
    which lets a real rally run and cuts a failed burst fast), or 100 bps
    below the entry fill price (hard stop). There is no take-profit: winners
    are left to run until the trailing stop takes them out.
  * No trade is ever placed during warm-up, and at most one order is sent per
    reply. Orders cap their own slippage at 100 bps past the latest close.

Everything is deterministic: closes are kept in arrival order in a plain
list, all arithmetic is plain float64 on those closes and on the fill prices
the harness reports, there is no randomness, no wall clock, no network and
no file access. Two runs on the same bars send byte-identical orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# 65 bps over the last three closes (the close now vs the close 3 bars back).
SURGE_BPS = 65
SURGE_BARS = 3
# 50 bps below the peak close since entry.
TRAIL_BPS = 50
# 100 bps below the entry fill price.
STOP_BPS = 100
# Slippage cap on every order, past the latest close.
MAX_SLIPPAGE_BPS = 100


class SurgeRider:
    """Keeps the closes seen so far and the entry/peak of the open position."""

    def __init__(self):
        # Closes in arrival order (price units, ints), one per bar received.
        self.closes = []
        # Fill price of the open position (price units), or None when flat.
        self.entry_price = None
        # Highest close seen since the entry fill, or None when flat.
        self.peak = None

    def on_batch(self, state, batch):
        # Fold the new fills in first: a buy fill sets the entry price, a
        # sell fill clears it. Fills arrive in the batch after the order.
        # The trailing stop is measured on closes, so the peak starts
        # empty and is set from the first close seen while holding.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.peak = None
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.peak = None

        # Record this batch's close (if the symbol had a bar this minute).
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)

        # Warm-up bars build history but never trade.
        if state.warming_up:
            return []

        held = state.positions.get(SYMBOL)
        if held is not None and held.qty > 0:
            return self._maybe_exit(state, close, held)
        return self._maybe_enter(state, close)

    def _maybe_exit(self, state, close, held):
        # The position the harness reports is the truth: if a fill we missed
        # opened it, anchor the entry at the position's cost instead.
        if self.entry_price is None:
            self.entry_price = self._cost_price(state, held)
        # Trailing stop on closes: the peak is the highest close since entry.
        if self.peak is None or close > self.peak:
            self.peak = close
        entry = self.entry_price
        if entry is None or entry <= 0:
            return []
        # Trailing stop: 50 bps under the peak close since entry.
        if (close - self.peak) * 10000 <= -TRAIL_BPS * self.peak:
            return self._sell_all(held)
        # Hard stop: 100 bps under the entry fill price.
        if (close - entry) * 10000 <= -STOP_BPS * entry:
            return self._sell_all(held)
        return []

    def _maybe_enter(self, state, close):
        if len(self.closes) <= SURGE_BARS:
            return []
        earlier = self.closes[-1 - SURGE_BARS]
        if earlier <= 0:
            return []
        # Surge: the close is at least 65 bps above the close 3 bars back.
        if (close - earlier) * 10000 < SURGE_BPS * earlier:
            return []
        room = state.weight_room(SYMBOL)
        spend = state.cash
        if room < spend:
            spend = room
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if spend < min_notional:
            return []
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": spend,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def _sell_all(self, held):
        self.entry_price = None
        self.peak = None
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    @staticmethod
    def _cost_price(state, held):
        """Entry price implied by the position's cost (price units).

        cost_quote_units is what the fills cost in micro-USDC; value =
        qty * price * 10**quote_scale / 10**(qty_scale + price_scale).
        """
        sym = state.symbols[SYMBOL]
        qty = held.qty
        cost = held.cost
        if qty <= 0 or cost <= 0:
            return None
        scale = state.hello["capital"]["quote_scale"]
        price = cost * 10 ** (int(sym["qty_scale"]) + int(sym["price_scale"]))
        price //= qty * 10 ** scale
        return price if price > 0 else None


def main():
    makefun_bars.run(SurgeRider().on_batch)


if __name__ == "__main__":
    main()
