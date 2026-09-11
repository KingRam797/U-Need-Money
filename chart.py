"""
chart.py — the reading surface.

Four stacked panels, because four things matter and they are not the same thing:

  1. PRICE (candles)   — what happened, and how violently
  2. VOLUME            — whether anyone actually agreed with it
  3. EQUITY            — your money vs. just holding
  4. DRAWDOWN          — the only panel that tells you if you could live with it

Read them bottom-up when you're deciding whether to trust a strategy.
Read them top-down when you're deciding what the market is doing.
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

UP, DOWN = "#26a69a", "#ef5350"
INK, GRID, BG = "#1a1a1a", "#e8e8e8", "#ffffff"


def _candles(ax, df):
    idx = np.arange(len(df))
    width = 0.62
    for i, (_, r) in enumerate(df.iterrows()):
        up = r["close"] >= r["open"]
        c = UP if up else DOWN
        ax.plot([i, i], [r["low"], r["high"]], color=c, linewidth=0.7, zorder=2)
        lo = min(r["open"], r["close"])
        h = abs(r["close"] - r["open"]) or (r["high"] - r["low"]) * 0.001
        ax.add_patch(plt.Rectangle((i - width / 2, lo), width, h,
                                   facecolor=c, edgecolor=c, linewidth=0.5, zorder=3))
    return idx


def render(df, broker, strat_name, symbol="BTCUSDT", interval="1m",
           last=250, out=None):
    """Render the last `last` candles plus the full equity history."""
    view = df.tail(last)
    out = out or Path(__file__).parent / f"chart_{strat_name}.png"

    fig, axes = plt.subplots(
        4, 1, figsize=(13, 11), sharex=False,
        gridspec_kw={"height_ratios": [3.2, 0.9, 2.0, 1.1], "hspace": 0.32},
        facecolor=BG,
    )
    ax_p, ax_v, ax_e, ax_d = axes
    for ax in axes:
        ax.set_facecolor(BG)
        ax.grid(True, color=GRID, linewidth=0.6, zorder=0)
        for s in ax.spines.values():
            s.set_color(GRID)
        ax.tick_params(colors="#666", labelsize=8)

    # ---- 1. price -------------------------------------------------------
    _candles(ax_p, view)
    for col, color, lbl in (("ema_fast", "#2962ff", "fast EMA"),
                            ("ema_slow", "#ff9800", "slow EMA"),
                            ("trend", "#7b1fa2", "trend SMA")):
        if col in view.columns:
            ax_p.plot(np.arange(len(view)), view[col].values,
                      color=color, linewidth=1.3, label=lbl, zorder=4)

    # trade markers, positioned onto the visible window
    tmap = {t: i for i, t in enumerate(view.index)}
    for f in broker.fills:
        if f.time in tmap:
            x = tmap[f.time]
            if f.side == "buy":
                ax_p.scatter(x, f.price * 0.998, marker="^", s=70,
                             color="#00897b", edgecolor="white",
                             linewidth=0.7, zorder=6)
            else:
                ax_p.scatter(x, f.price * 1.002, marker="v", s=70,
                             color="#c62828", edgecolor="white",
                             linewidth=0.7, zorder=6)

    ax_p.set_title(f"{symbol}  {interval}   ·   {strat_name}",
                   color=INK, fontsize=13, fontweight="600", loc="left", pad=12)
    ax_p.set_ylabel("price (USDT)", color="#555", fontsize=9)
    if ax_p.get_legend_handles_labels()[0]:
        ax_p.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax_p.set_xlim(-1, len(view))
    ax_p.set_xticks([])

    # ---- 2. volume ------------------------------------------------------
    cols = [UP if r["close"] >= r["open"] else DOWN for _, r in view.iterrows()]
    ax_v.bar(np.arange(len(view)), view["volume"].values,
             color=cols, width=0.62, zorder=3)
    ax_v.set_ylabel("volume", color="#555", fontsize=9)
    ax_v.set_xlim(-1, len(view))
    ax_v.set_xticks([])

    # ---- 3. equity vs buy & hold ---------------------------------------
    s = broker.summary()
    eq = s["equity_df"]
    start_eq = eq["equity"].iloc[0]
    bh = eq["price"] / eq["price"].iloc[0] * start_eq

    ax_e.plot(eq.index, eq["equity"], color="#1565c0",
              linewidth=1.7, label=strat_name, zorder=4)
    ax_e.plot(eq.index, bh, color="#9e9e9e", linewidth=1.3,
              linestyle="--", label="buy & hold", zorder=3)
    ax_e.axhline(start_eq, color="#bbb", linewidth=0.8, zorder=2)
    ax_e.fill_between(eq.index, start_eq, eq["equity"],
                      where=eq["equity"] >= start_eq, color="#1565c0",
                      alpha=0.10, zorder=1)
    ax_e.fill_between(eq.index, start_eq, eq["equity"],
                      where=eq["equity"] < start_eq, color=DOWN,
                      alpha=0.10, zorder=1)
    ax_e.set_ylabel("equity ($)", color="#555", fontsize=9)
    ax_e.legend(loc="upper left", fontsize=8, framealpha=0.9)

    # ---- 4. drawdown ----------------------------------------------------
    dd = (eq["equity"] / eq["equity"].cummax() - 1) * 100
    ax_d.fill_between(eq.index, dd, 0, color=DOWN, alpha=0.30, zorder=2)
    ax_d.plot(eq.index, dd, color=DOWN, linewidth=1.0, zorder=3)
    ax_d.set_ylabel("drawdown %", color="#555", fontsize=9)
    ax_d.axhline(dd.min(), color="#b71c1c", linewidth=0.8,
                 linestyle=":", zorder=4)
    ax_d.text(eq.index[0], dd.min(), f"  worst {dd.min():.1f}%",
              color="#b71c1c", fontsize=8, va="bottom")

    for ax in (ax_e, ax_d):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        plt.setp(ax.get_xticklabels(), rotation=0, ha="center")

    foot = (f"return {s['return_pct']:+.2f}%   ·   "
            f"buy&hold {s['buy_hold_pct']:+.2f}%   ·   "
            f"maxDD {s['max_drawdown_pct']:.1f}%   ·   "
            f"{s['n_trades']} trades   ·   "
            f"cost drag {s['cost_drag_pct']:.2f}%")
    fig.text(0.5, 0.055, foot, ha="center", color="#666", fontsize=9)
    fig.text(0.5, 0.028, "PAPER — no capital at risk", ha="center",
             color="#999", fontsize=8, style="italic")

    fig.savefig(out, dpi=140, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"chart -> {out}")
    return out
