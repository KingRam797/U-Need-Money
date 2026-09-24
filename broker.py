"""
broker.py — the honest part.

Most backtests lie in one of three places. This broker is built to refuse
all three:

  1. LOOKAHEAD. A signal computed from bar N's close cannot fill at bar N's
     close — you didn't know the close until it was over. Fills happen at
     bar N+1's OPEN. Always.
  2. FREE MONEY. Every fill pays a taker fee. Both sides. Every time.
  3. INFINITE LIQUIDITY. Every fill pays slippage: half the spread, plus an
     impact term that grows with how much of the bar's range you're eating.

Turn these off and any random strategy looks like genius. Leave them on and
most strategies die. That is the point of the rig.
"""

from dataclasses import dataclass, field


@dataclass
class Costs:
    """Defaults are Binance spot taker, no BNB discount, retail size."""
    fee_bps: float = 10.0          # 0.10% per side
    half_spread_bps: float = 1.0   # you cross the book; you pay half the spread
    impact_bps: float = 2.0        # baseline market impact
    impact_range_mult: float = 0.0 # extra bps per 1% of bar range (volatility tax)


@dataclass
class Fill:
    time: object
    side: str          # "buy" | "sell"
    qty: float
    price: float       # price actually received, after slippage
    ref_price: float   # the clean price you *thought* you'd get
    fee: float
    slip: float
    reason: str = ""


@dataclass
class PaperBroker:
    cash: float
    costs: Costs = field(default_factory=Costs)

    position: float = 0.0          # units of base asset (e.g. BTC)
    fills: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    fees_paid: float = 0.0
    slip_paid: float = 0.0

    # ---- internals -------------------------------------------------------

    def _slippage_bps(self, bar):
        bps = self.costs.half_spread_bps + self.costs.impact_bps
        if self.costs.impact_range_mult and bar is not None:
            rng_pct = (bar["high"] - bar["low"]) / bar["open"] * 100
            bps += self.costs.impact_range_mult * rng_pct
        return bps

    def _execute(self, when, side, qty, ref_price, bar, reason):
        if qty <= 0:
            return None
        slip_bps = self._slippage_bps(bar)
        # You always slip against yourself. Buying costs more, selling gets less.
        direction = 1 if side == "buy" else -1
        price = ref_price * (1 + direction * slip_bps / 10_000)

        notional = qty * price
        fee = notional * self.costs.fee_bps / 10_000
        slip = abs(price - ref_price) * qty

        if side == "buy":
            cost = notional + fee
            if cost > self.cash:                      # no phantom leverage
                qty = max(0.0, (self.cash / (1 + self.costs.fee_bps / 10_000)) / price)
                if qty <= 0:
                    return None
                notional = qty * price
                fee = notional * self.costs.fee_bps / 10_000
                slip = abs(price - ref_price) * qty
                cost = notional + fee
            self.cash -= cost
            self.position += qty
        else:
            qty = min(qty, self.position)             # spot only: no shorting
            if qty <= 0:
                return None
            notional = qty * price
            fee = notional * self.costs.fee_bps / 10_000
            slip = abs(price - ref_price) * qty
            self.cash += notional - fee
            self.position -= qty

        self.fees_paid += fee
        self.slip_paid += slip
        f = Fill(when, side, qty, price, ref_price, fee, slip, reason)
        self.fills.append(f)
        return f

    # ---- public API ------------------------------------------------------

    def target_weight(self, when, weight, ref_price, bar=None, reason=""):
        """
        Move the portfolio toward `weight` of equity held in the asset.
        weight=1.0 -> fully long. weight=0.0 -> fully in cash.
        This is the only way a strategy is allowed to touch the book.
        """
        weight = max(0.0, min(1.0, weight))
        equity = self.equity(ref_price)
        target_units = (equity * weight) / ref_price
        delta = target_units - self.position

        # Don't churn on dust — real exchanges have minimum notionals anyway.
        if abs(delta) * ref_price < max(10.0, equity * 0.001):
            return None
        side = "buy" if delta > 0 else "sell"
        return self._execute(when, side, abs(delta), ref_price, bar, reason)

    def equity(self, mark_price):
        return self.cash + self.position * mark_price

    def mark(self, when, price):
        self.equity_curve.append((when, self.equity(price), self.position, price))

    def summary(self, start_cash=None):
        import pandas as pd

        if not self.equity_curve:
            return {}
        eq = pd.DataFrame(
            self.equity_curve, columns=["time", "equity", "position", "price"]
        ).set_index("time")

        start = start_cash if start_cash is not None else eq["equity"].iloc[0]
        end = eq["equity"].iloc[-1]

        peak = eq["equity"].cummax()
        dd = (eq["equity"] / peak - 1)
        rets = eq["equity"].pct_change().dropna()

        # Round-trip P&L, so win rate means something.
        wins, losses, open_lot = [], [], None
        for f in self.fills:
            if f.side == "buy":
                open_lot = f
            elif open_lot is not None:
                pnl = (f.price - open_lot.price) * min(f.qty, open_lot.qty)
                (wins if pnl > 0 else losses).append(pnl)
                open_lot = None

        bh = (eq["price"].iloc[-1] / eq["price"].iloc[0] - 1) * 100

        return {
            "start_equity": start,
            "end_equity": end,
            "return_pct": (end / start - 1) * 100,
            "buy_hold_pct": bh,
            "max_drawdown_pct": dd.min() * 100,
            "n_trades": len(self.fills),
            "n_round_trips": len(wins) + len(losses),
            "win_rate_pct": (100 * len(wins) / max(1, len(wins) + len(losses))),
            "fees_paid": self.fees_paid,
            "slippage_paid": self.slip_paid,
            "cost_drag_pct": (self.fees_paid + self.slip_paid) / start * 100,
            "vol_pct": rets.std() * 100,
            "equity_df": eq,
        }
