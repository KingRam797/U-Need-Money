"""One-symbol, long-only Jev paper trader. No authenticated exchange client.

Decide from the latest *closed* candle. A proposed change is filled only at
the next candle's open; missing that candle cancels the pending change.
SQLite persists the portfolio and audit trail across process restarts.
"""

import argparse
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import feed
from broker import PaperBroker
from strategy import atr, ema, rsi

DB_PATH = Path(__file__).parent / "jev_paper.db"
MODEL = "jev-1.13.0"
PRICE_PER_MILLION_INPUT_TOKENS = 0.042  # Check before changing the model.
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "30m": 1800}


@dataclass(frozen=True)
class Limits:
    max_weight: float = 0.20
    max_spread_bps: float = 15.0
    max_daily_loss_usd: float = 1.0
    max_drawdown_pct: float = 5.0
    max_calls_per_day: int = 96
    max_jev_spend_usd: float = 0.05
    min_choice_probability: float = 0.65


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def ask_jev(snapshot, api_key, *, post=requests.post):
    """Typed choice only. A malformed or unavailable response raises; caller holds."""
    body = {
        "model": MODEL,
        "state": snapshot,
        "questions": {"action": {
            "type": "choice",
            "instructions": (
                "Choose the next spot-paper portfolio action using only this closed-candle "
                "snapshot. Trading fees and spread matter. Uncertain evidence favors HOLD. "
                "BUY opens or maintains a small long, SELL exits an existing long."
            ),
            "criteria": {
                "BUY": "Evidence favors owning the asset over the next interval.",
                "SELL": "Evidence favors exiting an existing long position.",
                "HOLD": "Keep the current position; evidence is insufficient to change it.",
            },
        }},
    }
    response = post(
        "https://api.typesafe.ai/v1/systemone",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body, timeout=5,
    )
    response.raise_for_status()
    payload = response.json()
    answer = payload["answers"]["action"]
    probabilities = answer["probabilities"]
    if answer["type"] != "choice" or set(probabilities) != {"BUY", "SELL", "HOLD"}:
        raise ValueError("invalid choice response")
    if not all(_finite(p) and 0 <= p <= 1 for p in probabilities.values()):
        raise ValueError("invalid probabilities")
    if abs(sum(probabilities.values()) - 1) > 0.02:
        raise ValueError("probabilities do not sum to one")
    choice = answer["choice"]
    if choice not in probabilities or choice != max(probabilities, key=probabilities.get):
        raise ValueError("choice does not match probabilities")
    confidence = answer["confidence"]
    if not _finite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("invalid confidence")
    tokens = payload["usage"]["input_tokens"]
    if not isinstance(tokens, int) or tokens < 0 or tokens > 64000:
        raise ValueError("invalid usage")
    return {"choice": choice, "probabilities": probabilities,
            "confidence": confidence, "input_tokens": tokens,
            "model": payload["model"]}


def snapshot_of(closed, position, equity, symbol):
    """All features use the caller-provided closed candles only."""
    frame = closed.tail(210)
    row = frame.iloc[-1]
    closes = frame["close"]
    def number(x):
        return round(float(x), 6) if pd.notna(x) and math.isfinite(float(x)) else None
    return {
        "symbol": symbol, "candle_time": frame.index[-1].isoformat(),
        "close": number(row["close"]), "volume": number(row["volume"]),
        "ema_12": number(ema(closes, 12).iloc[-1]),
        "ema_48": number(ema(closes, 48).iloc[-1]),
        "rsi_14": number(rsi(closes, 14).iloc[-1]),
        "atr_14": number(atr(frame, 14).iloc[-1]),
        "position_units": number(position), "paper_equity_usd": number(equity),
    }


class PaperSession:
    def __init__(self, path, symbol, interval, start_cash, limits=Limits()):
        if interval not in INTERVAL_SECONDS or start_cash <= 0:
            raise ValueError("unsupported interval or nonpositive starting cash")
        self.symbol, self.interval, self.limits = symbol, interval, limits
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS events (candle TEXT PRIMARY KEY, data TEXT NOT NULL)")
        row = self.conn.execute("SELECT data FROM state WHERE id=1").fetchone()
        if row:
            self.state = json.loads(row[0])
            if (self.state["symbol"], self.state["interval"]) != (symbol, interval):
                raise ValueError("existing session uses a different symbol or interval")
        else:
            if start_cash * limits.max_weight < 10:
                raise ValueError("paper cash × max weight must reach the broker's $10 dust floor")
            self.state = {
                "symbol": symbol, "interval": interval, "cash": start_cash,
                "position": 0.0, "last_candle": None, "pending_weight": None,
                "peak_equity": start_cash, "day": None, "day_start_equity": start_cash,
                "calls_today": 0, "jev_spend_today": 0.0,
            }
            with self.conn:
                self.conn.execute("INSERT INTO state VALUES (1, ?)", (json.dumps(self.state),))

    def close(self):
        self.conn.close()

    def step(self, closed, spread_bps, api_key, *, decider=ask_jev, now=None):
        """Process only the latest closed candle once. Returns an audit event."""
        if len(closed) < 55 or not closed.index.is_monotonic_increasing:
            raise ValueError("need at least 55 ordered closed candles")
        ts = closed.index[-1]
        if ts.tzinfo is None:
            raise ValueError("candles must have timezone-aware timestamps")
        candle = ts.isoformat()
        previous = self.state["last_candle"]
        if previous and ts <= pd.Timestamp(previous):
            return None
        now = now or datetime.now(timezone.utc)
        seconds = INTERVAL_SECONDS[self.interval]
        broker = PaperBroker(cash=self.state["cash"], position=self.state["position"])
        event = {"candle": candle, "snapshot": None, "choice": "HOLD", "reason": None,
                 "fill": None, "model": None, "probabilities": None, "input_tokens": 0}
        # A signal from N can fill only at the open of N+1. Never backfill
        # missed bars on restart or pretend a stale signal was executed.
        consecutive = previous is not None and (ts - pd.Timestamp(previous)).total_seconds() == seconds
        age = (now - ts.to_pydatetime()).total_seconds()
        fresh = 0 <= age <= seconds * 2
        spread_ok = _finite(spread_bps) and 0 <= spread_bps <= self.limits.max_spread_bps
        if consecutive and fresh and spread_ok and self.state["pending_weight"] is not None:
            nxt = closed.iloc[-1]
            fill = broker.target_weight(ts, self.state["pending_weight"], float(nxt["open"]),
                                        bar=nxt, reason="previous closed candle")
            if fill:
                event["fill"] = {"side": fill.side, "qty": fill.qty, "price": fill.price,
                                 "fee": fill.fee, "slip": fill.slip}
        self.state["pending_weight"] = None
        price = float(closed.iloc[-1]["close"])
        equity = broker.equity(price)
        day = now.astimezone(timezone.utc).date().isoformat()
        if self.state["day"] != day:
            self.state.update(day=day, day_start_equity=equity, calls_today=0, jev_spend_today=0.0)
        self.state["peak_equity"] = max(self.state["peak_equity"], equity)
        event["snapshot"] = snapshot_of(closed, broker.position, equity, self.symbol)
        if not fresh:
            event["reason"] = "stale_data"
        elif not spread_ok:
            event["reason"] = "spread_limit"
        elif equity <= self.state["day_start_equity"] - self.limits.max_daily_loss_usd:
            event["reason"] = "daily_loss_limit"
        elif equity <= self.state["peak_equity"] * (1 - self.limits.max_drawdown_pct / 100):
            event["reason"] = "drawdown_limit"
        elif self.state["calls_today"] >= self.limits.max_calls_per_day or self.state["jev_spend_today"] >= self.limits.max_jev_spend_usd:
            event["reason"] = "jev_budget_limit"
        elif not api_key:
            event["reason"] = "missing_jev_key"
        else:
            # Count attempted calls before contacting the model. A timeout can
            # still incur charges; the request count remains bounded.
            self.state["calls_today"] += 1
            try:
                answer = decider(event["snapshot"], api_key)
                event.update(choice=answer["choice"], probabilities=answer["probabilities"],
                             model=answer["model"], input_tokens=answer["input_tokens"])
                self.state["jev_spend_today"] += answer["input_tokens"] * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
                probability = answer["probabilities"][answer["choice"]]
                if probability < self.limits.min_choice_probability:
                    event["reason"] = "low_probability"
                elif answer["choice"] == "BUY":
                    self.state["pending_weight"] = self.limits.max_weight
                    event["reason"] = "pending_buy"
                elif answer["choice"] == "SELL" and broker.position > 0:
                    self.state["pending_weight"] = 0.0
                    event["reason"] = "pending_sell"
                else:
                    event["reason"] = "hold"
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                event["reason"] = f"jev_error:{type(exc).__name__}"
        self.state.update(cash=broker.cash, position=broker.position, last_candle=candle)
        event["equity"] = equity
        event["spread_bps"] = spread_bps if _finite(spread_bps) else None
        with self.conn:
            self.conn.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(self.state),))
            self.conn.execute("INSERT INTO events VALUES (?, ?)", (candle, json.dumps(event)))
        return event


def main():
    parser = argparse.ArgumentParser(description="Jev spot paper trading; no real orders")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", choices=INTERVAL_SECONDS, default="15m")
    parser.add_argument("--cash", type=float, default=80.0, help="simulated starting cash")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--market-base", choices=["https://api.binance.com", "https://api.binance.us"],
                        default="https://api.binance.us")
    parser.add_argument("--once", action="store_true", help="process one closed candle and exit")
    args = parser.parse_args()
    feed.BINANCE_BASE = args.market_base
    session = PaperSession(args.db, args.symbol, args.interval, args.cash)
    print(f"Jev paper session: {args.symbol} {args.interval}; DB={args.db}; Ctrl-C to stop")
    try:
        while True:
            try:
                bars = feed.fetch_klines(args.symbol, args.interval, limit=250)
                bars["time"] = pd.to_datetime(bars["open_ms"], unit="ms", utc=True)
                closed = bars.set_index("time").drop(columns=["open_ms"]).iloc[:-1]
                event = session.step(closed, feed.spread_bps(args.symbol), os.getenv("TYPESAFE_API_KEY"))
                if event:
                    print(f"{event['candle']} {event['choice']} {event['reason']} "
                          f"equity=${event['equity']:.2f} fill={event['fill']}")
            except requests.RequestException as exc:
                print(f"Market data error ({type(exc).__name__}); no decision")
            if args.once:
                break
            time.sleep(20)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
