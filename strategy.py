"""
strategy.py — where your ideas go.

A strategy sees ONLY the candles up to and including bar N, and returns a
target weight between 0.0 (all cash) and 1.0 (all in). The engine fills it
at bar N+1's open. That structure makes lookahead bias physically impossible
rather than merely discouraged.

The two strategies here are teaching examples, not recommendations.
EMACross in particular is one of the most thoroughly arbitraged-away ideas
in existence. It is here so you can watch costs eat it.
"""

import numpy as np
import pandas as pd


# ---- indicator library (all causal — no future data) ---------------------

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def sma(series, window):
    return series.rolling(window).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    """Average True Range — the volatility number that should size your risk."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(series, window=20, mult=2.0):
    mid = sma(series, window)
    sd = series.rolling(window).std()
    return mid - mult * sd, mid, mid + mult * sd


def realized_vol(series, window=60):
    """Annualized-ish realized vol from log returns. Scale-free risk read."""
    lr = np.log(series / series.shift())
    return lr.rolling(window).std() * np.sqrt(365 * 24 * 60 / 1)


# ---- strategies ----------------------------------------------------------

class Strategy:
    name = "base"
    warmup = 50

    def prepare(self, df):
        """Compute indicators once, vectorized, over the whole frame."""
        return df

    def weight(self, df, i):
        """Target weight given data up to and including row i. 0.0–1.0."""
        raise NotImplementedError


class BuyHold(Strategy):
    """The benchmark you must beat. Most strategies do not."""
    name = "buy_hold"
    warmup = 1

    def weight(self, df, i):
        return 1.0


class EMACross(Strategy):
    """Long when fast EMA > slow EMA, flat otherwise. The classic."""
    name = "ema_cross"

    def __init__(self, fast=12, slow=48):
        self.fast, self.slow = fast, slow
        self.warmup = slow + 5

    def prepare(self, df):
        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.fast)
        df["ema_slow"] = ema(df["close"], self.slow)
        return df

    def weight(self, df, i):
        row = df.iloc[i]
        if np.isnan(row["ema_slow"]):
            return 0.0
        return 1.0 if row["ema_fast"] > row["ema_slow"] else 0.0


class VolTargeted(Strategy):
    """
    Long-only, but sized inversely to recent volatility, and stands down
    when price is below its slow trend. Not a money printer — an example of
    the only edge retail reliably has: risk sizing, not prediction.
    """
    name = "vol_targeted"

    def __init__(self, trend=200, vol_window=60, target_vol=0.60):
        self.trend, self.vol_window, self.target_vol = trend, vol_window, target_vol
        self.warmup = max(trend, vol_window) + 5

    def prepare(self, df):
        df = df.copy()
        df["trend"] = sma(df["close"], self.trend)
        df["rvol"] = realized_vol(df["close"], self.vol_window)
        return df

    def weight(self, df, i):
        row = df.iloc[i]
        if np.isnan(row["trend"]) or np.isnan(row["rvol"]) or row["rvol"] <= 0:
            return 0.0
        if row["close"] < row["trend"]:
            return 0.0
        return float(np.clip(self.target_vol / row["rvol"], 0.0, 1.0))


REGISTRY = {
    "buy_hold": BuyHold,
    "ema_cross": EMACross,
    "vol_targeted": VolTargeted,
}
