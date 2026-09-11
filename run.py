"""
run.py — the engine.

Two modes:

  python run.py replay --strategy ema_cross --symbol BTCUSDT --interval 1m
      Runs the strategy over cached history. Fast. Use this to kill bad ideas.

  python run.py live --strategy vol_targeted --interval 1m
      Polls Binance on each new candle close and paper-trades forward in real
      time, appending to paper_log.csv. No keys. No orders. No money.

Nothing in this repository can place a real trade. There is no exchange
credential anywhere in it, by design.
"""

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import feed
from broker import Costs, PaperBroker
from strategy import REGISTRY

LOG = Path(__file__).parent / "paper_log.csv"


def run_replay(df, strat, start_cash=1000.0, costs=None):
    """Walk the frame bar by bar. Signal on bar i, fill on bar i+1's open."""
    df = strat.prepare(df)
    bk = PaperBroker(cash=start_cash, costs=costs or Costs())

    for i in range(strat.warmup, len(df) - 1):
        w = strat.weight(df, i)
        nxt = df.iloc[i + 1]
        bk.target_weight(
            when=df.index[i + 1],
            weight=w,
            ref_price=nxt["open"],       # the price you could actually get
            bar=nxt,
            reason=f"w={w:.2f}",
        )
        bk.mark(df.index[i + 1], nxt["close"])

    return bk, df


def print_summary(s, strat_name):
    print(f"\n{'=' * 58}")
    print(f"  {strat_name}")
    print(f"{'=' * 58}")
    print(f"  Start equity      ${s['start_equity']:,.2f}")
    print(f"  End equity        ${s['end_equity']:,.2f}")
    print(f"  Strategy return   {s['return_pct']:+.2f}%")
    print(f"  Buy & hold        {s['buy_hold_pct']:+.2f}%   <- the bar to clear")
    print(f"  Max drawdown      {s['max_drawdown_pct']:.2f}%")
    print(f"  Trades            {s['n_trades']}  ({s['n_round_trips']} round trips)")
    print(f"  Win rate          {s['win_rate_pct']:.1f}%")
    print(f"  Fees paid         ${s['fees_paid']:,.2f}")
    print(f"  Slippage paid     ${s['slippage_paid']:,.2f}")
    print(f"  Total cost drag   {s['cost_drag_pct']:.2f}% of starting capital")
    print(f"{'=' * 58}\n")


def run_live(strat, symbol, interval, start_cash, poll=20):
    """Poll for new closed candles and paper-trade forward. Ctrl-C to stop."""
    bk = PaperBroker(cash=start_cash)
    seen = None
    new = not LOG.exists()
    fh = open(LOG, "a", newline="")
    w = csv.writer(fh)
    if new:
        w.writerow(["ts", "symbol", "price", "weight", "position",
                    "cash", "equity", "spread_bps", "action"])

    print(f"Live paper trading {symbol} {interval} | {strat.name} | "
          f"${start_cash:,.2f} | Ctrl-C to stop\n")

    try:
        while True:
            df = feed.fetch_klines(symbol, interval, limit=500)
            df["time"] = pd.to_datetime(df["open_ms"], unit="ms", utc=True)
            df = df.set_index("time").drop(columns=["open_ms"])
            closed = df.iloc[:-1]           # last candle is still forming
            if len(closed) < strat.warmup + 2:
                time.sleep(poll)
                continue

            last_ts = closed.index[-1]
            if last_ts == seen:
                time.sleep(poll)
                continue
            seen = last_ts

            prep = strat.prepare(closed)
            weight = strat.weight(prep, len(prep) - 1)
            px = feed.latest_price(symbol)
            sp = feed.spread_bps(symbol)

            fill = bk.target_weight(last_ts, weight, px, reason="live")
            bk.mark(last_ts, px)
            eq = bk.equity(px)

            action = "hold"
            if fill:
                action = f"{fill.side} {fill.qty:.6f} @ {fill.price:,.2f}"

            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{stamp}] {px:>12,.2f}  w={weight:4.2f}  "
                  f"eq=${eq:>10,.2f}  spread={sp:4.1f}bps  {action}")

            w.writerow([last_ts, symbol, px, round(weight, 4),
                        round(bk.position, 8), round(bk.cash, 2),
                        round(eq, 2), round(sp, 2), action])
            fh.flush()
            time.sleep(poll)

    except KeyboardInterrupt:
        print("\nStopped.")
        s = bk.summary(start_cash)
        if s:
            print_summary(s, f"{strat.name} (live session)")
    finally:
        fh.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["replay", "live"])
    p.add_argument("--strategy", default="ema_cross", choices=list(REGISTRY))
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--cash", type=float, default=1000.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--chart", action="store_true", help="render a PNG after replay")
    a = p.parse_args()

    strat = REGISTRY[a.strategy]()
    costs = Costs(fee_bps=a.fee_bps)

    if a.mode == "replay":
        df = feed.load(a.symbol, a.interval)
        if df.empty:
            print("No cached data. Run:  python feed.py BTCUSDT 1m 5000")
            return
        print(f"Replaying {len(df)} candles: {df.index[0]} -> {df.index[-1]}")

        bh_bk, _ = run_replay(df, REGISTRY["buy_hold"](), a.cash, costs)
        print_summary(bh_bk.summary(a.cash), "buy_hold (benchmark)")

        bk, prepped = run_replay(df, strat, a.cash, costs)
        s = bk.summary(a.cash)
        print_summary(s, strat.name)

        if a.chart:
            import chart
            chart.render(prepped, bk, strat.name, a.symbol, a.interval)
    else:
        run_live(strat, a.symbol, a.interval, a.cash)


if __name__ == "__main__":
    main()
