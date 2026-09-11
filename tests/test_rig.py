"""
The demo replay is deterministic: _seed_demo.py draws from a fixed RNG seed,
so ema_cross over DEMOUSDT lands on the same numbers every run. That makes it
a regression test for the whole engine — feed, broker, strategy and run loop
at once.

These tests also pin the two invariants the rig exists to enforce: fills land
on the NEXT bar's open (no lookahead), and every fill pays fees and slippage.
"""

import runpy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import chart
import feed
import run
from broker import Costs, PaperBroker
from strategy import REGISTRY, Strategy

ROOT = Path(__file__).resolve().parent.parent
START_CASH = 1000.0

# Figures the README documents. Tolerances absorb floating-point drift across
# numpy/pandas versions; they are far tighter than any real regression.
EXPECTED = {
    "return_pct": -11.35,
    "buy_hold_pct": 2.06,
    "max_drawdown_pct": -14.53,
    "cost_drag_pct": 12.81,
    "n_trades": 107,
}
TOL_PCT = 0.05


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory):
    """Seed synthetic candles into a throwaway DB, not the developer's cache."""
    feed.DB_PATH = tmp_path_factory.mktemp("rig") / "market.db"
    runpy.run_path(str(ROOT / "_seed_demo.py"), run_name="__seed__")
    return feed.DB_PATH


@pytest.fixture(scope="session")
def demo_candles(seeded_db):
    df = feed.load("DEMOUSDT", "1m")
    assert len(df) == 4000, f"expected 4000 seeded candles, got {len(df)}"
    return df


@pytest.fixture(scope="session")
def ema_replay(demo_candles):
    bk, prepped = run.run_replay(
        demo_candles, REGISTRY["ema_cross"](), START_CASH, Costs()
    )
    return bk, prepped


def test_seeded_candles_are_well_formed(demo_candles):
    df = demo_candles
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates
    assert (df["high"] >= df["low"]).all()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["volume"] > 0).all()


@pytest.mark.parametrize("metric", sorted(EXPECTED))
def test_ema_cross_reproduces_documented_result(ema_replay, metric):
    summary = ema_replay[0].summary(START_CASH)
    expected = EXPECTED[metric]
    if metric == "n_trades":
        assert summary[metric] == expected
    else:
        assert summary[metric] == pytest.approx(expected, abs=TOL_PCT)


def test_ema_cross_loses_to_buy_and_hold(ema_replay):
    """The README's central lesson. If this flips, the cost model broke."""
    s = ema_replay[0].summary(START_CASH)
    assert s["return_pct"] < 0 < s["buy_hold_pct"]
    assert s["cost_drag_pct"] > 10


class _Alternating(Strategy):
    """Flips between all-in and all-cash every bar, to force a fill on each one."""

    name = "alternating"
    warmup = 1

    def weight(self, df, i):
        return float(i % 2)


@pytest.fixture(scope="session")
def gapped_candles():
    """
    Candles that GAP: every bar opens away from the previous close.

    The seeded demo data is gapless by construction (each open is the prior
    close), so on it "next open" and "this close" are the same number and a
    lookahead bug would hide. These bars make the two distinguishable.
    """
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    close = 100 + np.sin(np.arange(n) / 3) * 5
    opens = close + 7.0          # never equal to the previous close
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, close) + 1.0,
            "low": np.minimum(opens, close) - 1.0,
            "close": close,
            "volume": np.full(n, 10.0),
        },
        index=idx,
    )


def test_gapped_fixture_actually_gaps(gapped_candles):
    """Guard the guard: if these bars stopped gapping, the test below goes blind."""
    df = gapped_candles
    assert (df["open"].shift(-1) != df["close"]).iloc[:-1].all()


def test_no_lookahead(gapped_candles):
    """
    Every fill must reference the open of the bar it is stamped with. A signal
    from bar N filling at bar N's close would break this, which is exactly the
    cheat the rig refuses.
    """
    bk, prepped = run.run_replay(gapped_candles, _Alternating(), START_CASH, Costs())
    opens, closes = prepped["open"], prepped["close"]
    assert bk.fills, "expected the strategy to trade"
    for f in bk.fills:
        assert f.time in opens.index
        assert f.ref_price == pytest.approx(float(opens.loc[f.time]))
        assert f.ref_price != pytest.approx(float(closes.loc[f.time]))


def test_fills_reference_the_bar_after_the_signal(ema_replay):
    """Fills are stamped on a bar strictly later than the first tradable one."""
    bk, prepped = ema_replay
    first_tradable = prepped.index[REGISTRY["ema_cross"]().warmup + 1]
    assert bk.fills
    for f in bk.fills:
        assert f.time >= first_tradable


def test_every_fill_pays_costs(ema_replay):
    bk = ema_replay[0]
    for f in bk.fills:
        assert f.fee > 0
        assert f.slip > 0
        # You always slip against yourself.
        assert (f.price > f.ref_price) if f.side == "buy" else (f.price < f.ref_price)


def test_no_shorting_and_no_leverage(ema_replay):
    bk, prepped = ema_replay
    assert bk.position >= 0
    assert bk.cash >= -1e-9
    for _, equity, position, _ in bk.equity_curve:
        assert position >= 0
        assert equity > 0


def test_zero_cost_run_beats_the_costed_one(demo_candles):
    """Isolates cost drag as the cause of the loss, not the signal."""
    free = Costs(fee_bps=0.0, half_spread_bps=0.0, impact_bps=0.0)
    bk, _ = run.run_replay(demo_candles, REGISTRY["ema_cross"](), START_CASH, free)
    s = bk.summary(START_CASH)
    assert s["cost_drag_pct"] == pytest.approx(0.0, abs=1e-9)
    assert s["return_pct"] > EXPECTED["return_pct"]


def test_buy_hold_tracks_the_market_and_barely_trades(demo_candles):
    bk, _ = run.run_replay(demo_candles, REGISTRY["buy_hold"](), START_CASH, Costs())
    s = bk.summary(START_CASH)
    assert s["n_trades"] == 1
    assert s["return_pct"] == pytest.approx(s["buy_hold_pct"], abs=0.5)


def test_vol_targeted_replays(demo_candles):
    bk, _ = run.run_replay(
        demo_candles, REGISTRY["vol_targeted"](), START_CASH, Costs()
    )
    s = bk.summary(START_CASH)
    assert s["n_trades"] > 0
    assert 0 < s["end_equity"]


def test_strategy_weights_stay_in_range(demo_candles):
    for name, cls in REGISTRY.items():
        strat = cls()
        prepped = strat.prepare(demo_candles)
        for i in (strat.warmup, len(prepped) // 2, len(prepped) - 1):
            w = strat.weight(prepped, i)
            assert 0.0 <= w <= 1.0, f"{name} returned weight {w} at bar {i}"


def test_chart_renders(ema_replay, tmp_path):
    bk, prepped = ema_replay
    out = tmp_path / "chart.png"
    chart.render(prepped, bk, "ema_cross", "DEMOUSDT", "1m", out=out)
    assert out.exists()
    assert out.stat().st_size > 10_000


def test_feed_module_cannot_trade():
    """The repo's core promise: no code path places a real order."""
    source = (ROOT / "feed.py").read_text()
    for forbidden in ("api_key", "apiKey", "signature", "/api/v3/order", "secret"):
        assert forbidden not in source, f"feed.py mentions {forbidden!r}"


# ---- broker guards ------------------------------------------------------
#
# target_weight clips to 0..1, so these two guards never bind through the
# normal path. They are the floor under a future strategy that sizes its own
# orders, so they get exercised directly rather than left to trust.


def test_cannot_sell_more_than_held():
    bk = PaperBroker(cash=0.0, costs=Costs())
    bk.position = 1.0
    bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    fill = bk._execute("t", "sell", 5.0, 100.0, bar, "oversized sell")
    assert fill.qty == pytest.approx(1.0)
    assert bk.position == pytest.approx(0.0)
    assert bk.position >= 0


def test_cannot_buy_beyond_cash():
    bk = PaperBroker(cash=1_000.0, costs=Costs())
    bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    fill = bk._execute("t", "buy", 50.0, 100.0, bar, "oversized buy")
    assert bk.cash >= -1e-9, "spent more cash than it had"
    assert fill.qty * fill.price + fill.fee == pytest.approx(1_000.0, rel=1e-6)
