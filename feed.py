"""
feed.py — market data in, nothing else.

Pulls OHLCV candles from Binance's public REST endpoint (no API key needed,
no account needed) and caches them in SQLite so you never re-download a
candle twice.

Public data only. This file has no ability to place an order.
"""

import sqlite3
import time
from pathlib import Path

import pandas as pd
import requests

BINANCE_BASE = "https://api.binance.com"
DB_PATH = Path(__file__).parent / "market.db"

# Binance caps each klines request at 1000 candles.
MAX_LIMIT = 1000


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            symbol   TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_ms  INTEGER NOT NULL,
            open     REAL, high REAL, low REAL, close REAL, volume REAL,
            trades   INTEGER,
            PRIMARY KEY (symbol, interval, open_ms)
        )
        """
    )
    return conn


def fetch_klines(symbol="BTCUSDT", interval="1m", limit=1000, end_ms=None):
    """One raw call to Binance. Returns a DataFrame, oldest first."""
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, MAX_LIMIT)}
    if end_ms:
        params["endTime"] = int(end_ms)
    r = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=15)
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(
        rows,
        columns=[
            "open_ms", "open", "high", "low", "close", "volume",
            "close_ms", "quote_volume", "trades",
            "taker_base", "taker_quote", "ignore",
        ],
    )
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["trades"] = df["trades"].astype(int)
    df["open_ms"] = df["open_ms"].astype("int64")
    return df[["open_ms", "open", "high", "low", "close", "volume", "trades"]]


def backfill(symbol="BTCUSDT", interval="1m", candles=5000, pause=0.25):
    """
    Walk backwards from now until `candles` candles are cached.
    Rate-limit friendly: Binance allows plenty, but we sleep anyway.
    """
    conn = _db()
    got, end_ms = 0, None
    while got < candles:
        batch = fetch_klines(symbol, interval, MAX_LIMIT, end_ms)
        if batch.empty:
            break
        batch.insert(0, "interval", interval)
        batch.insert(0, "symbol", symbol)
        conn.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)",
            batch.itertuples(index=False, name=None),
        )
        conn.commit()
        got += len(batch)
        end_ms = int(batch["open_ms"].iloc[0]) - 1
        print(f"  cached {got}/{candles} {symbol} {interval}")
        time.sleep(pause)
    conn.close()
    return got


def load(symbol="BTCUSDT", interval="1m", limit=None):
    """Read cached candles out as a clean, time-indexed DataFrame."""
    conn = _db()
    df = pd.read_sql_query(
        "SELECT open_ms,open,high,low,close,volume,trades FROM candles "
        "WHERE symbol=? AND interval=? ORDER BY open_ms ASC",
        conn, params=(symbol, interval),
    )
    conn.close()
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["open_ms"], unit="ms", utc=True)
    df = df.set_index("time").drop(columns=["open_ms"])
    return df.tail(limit) if limit else df


def latest_price(symbol="BTCUSDT"):
    r = requests.get(
        f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=10
    )
    r.raise_for_status()
    return float(r.json()["price"])


def spread_bps(symbol="BTCUSDT"):
    """
    Live bid/ask spread in basis points. This is the number that decides
    whether a 'gap' is real profit or just the cost of crossing the book.
    """
    r = requests.get(
        f"{BINANCE_BASE}/api/v3/ticker/bookTicker",
        params={"symbol": symbol}, timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    bid, ask = float(d["bidPrice"]), float(d["askPrice"])
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10_000


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    iv = sys.argv[2] if len(sys.argv) > 2 else "1m"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
    print(f"Backfilling {n} x {iv} candles of {sym}...")
    backfill(sym, iv, n)
    df = load(sym, iv)
    print(f"\nCached {len(df)} candles: {df.index[0]} -> {df.index[-1]}")
    print(df.tail(3))
