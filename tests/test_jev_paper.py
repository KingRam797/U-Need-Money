"""Paper Jev integration: no real API calls or exchange credentials."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from jev_paper import Limits, PaperSession, ask_jev


@pytest.fixture
def candles():
    start = pd.Timestamp.now(tz="UTC").floor("15min") - pd.Timedelta(minutes=15 * 70)
    index = pd.date_range(start, periods=70, freq="15min", tz="UTC")
    close = 100 + np.arange(70) * 0.01
    return pd.DataFrame({"open": close, "high": close + 0.2,
                         "low": close - 0.2, "close": close,
                         "volume": np.full(70, 10.0)}, index=index)


def fake_decision(choice):
    def decide(snapshot, key):
        assert key == "test-key"
        return {"choice": choice, "probabilities": {x: (1.0 if x == choice else 0.0)
                for x in ("BUY", "SELL", "HOLD")}, "confidence": 1.0, "input_tokens": 400,
                "model": "jev-1.13.0"}
    return decide


def test_next_open_and_restart_are_idempotent(candles, tmp_path):
    path = tmp_path / "session.db"
    s = PaperSession(path, "BTCUSDT", "15m", 80)
    first = candles.iloc[:69]
    now = first.index[-1].to_pydatetime() + timedelta(minutes=15)
    event = s.step(first, 1, "test-key", decider=fake_decision("BUY"), now=now)
    assert event["fill"] is None
    assert s.state["position"] == 0
    s.close()

    s = PaperSession(path, "BTCUSDT", "15m", 9999)  # cash argument never resets a session
    second = candles
    now = second.index[-1].to_pydatetime() + timedelta(minutes=15)
    event = s.step(second, 1, "test-key", decider=fake_decision("HOLD"), now=now)
    assert event["fill"]["side"] == "buy"
    assert event["fill"]["price"] > second.iloc[-1]["open"]
    assert event["fill"]["qty"] * event["fill"]["price"] <= 80 * 0.21
    assert s.step(second, 1, "test-key", decider=fake_decision("BUY"), now=now) is None
    assert s.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert s.state["calls_today"] == 2
    s.close()


def test_small_paper_account_cannot_silently_make_no_trades(tmp_path):
    with pytest.raises(ValueError, match="dust floor"):
        PaperSession(tmp_path / "too-small.db", "BTCUSDT", "15m", 20)


def test_missing_bar_cancels_pending_order(candles, tmp_path):
    s = PaperSession(tmp_path / "gap.db", "BTCUSDT", "15m", 80)
    s.step(candles.iloc[:68], 1, "test-key", decider=fake_decision("BUY"),
           now=candles.index[67].to_pydatetime() + timedelta(minutes=15))
    event = s.step(candles, 1, "test-key", decider=fake_decision("HOLD"),
                   now=candles.index[-1].to_pydatetime() + timedelta(minutes=15))
    assert event["fill"] is None
    assert s.state["position"] == 0
    s.close()


@pytest.mark.parametrize("reason,spread,key,age", [
    ("spread_limit", 20.0, "test-key", 900),
    ("missing_jev_key", 1.0, None, 900),
    ("stale_data", 1.0, "test-key", 1900),
])
def test_guards_prevent_decision(candles, tmp_path, reason, spread, key, age):
    s = PaperSession(tmp_path / "guard.db", "BTCUSDT", "15m", 80)
    def forbidden(*args):
        raise AssertionError("Jev must not be called")
    event = s.step(candles, spread, key, decider=forbidden,
                   now=candles.index[-1].to_pydatetime() + timedelta(seconds=age))
    assert event["reason"] == reason
    assert event["fill"] is None
    s.close()


def test_failure_and_low_probability_hold(candles, tmp_path):
    s = PaperSession(tmp_path / "failure.db", "BTCUSDT", "15m", 80, Limits(max_calls_per_day=1))
    def fails(*args):
        raise ValueError("bad model response")
    event = s.step(candles.iloc[:69], 1, "test-key", decider=fails,
                   now=candles.index[-2].to_pydatetime() + timedelta(minutes=15))
    assert event["reason"] == "jev_error:ValueError"
    assert s.state["calls_today"] == 1
    event = s.step(candles, 1, "test-key", decider=fake_decision("BUY"),
                   now=candles.index[-1].to_pydatetime() + timedelta(minutes=15))
    assert event["reason"] == "jev_budget_limit"
    assert s.state["pending_weight"] is None
    s.close()


def test_pending_buy_canceled_when_spread_widens(candles, tmp_path):
    s = PaperSession(tmp_path / "spread.db", "BTCUSDT", "15m", 80)
    s.step(candles.iloc[:69], 1, "test-key", decider=fake_decision("BUY"),
           now=candles.index[-2].to_pydatetime() + timedelta(minutes=15))
    event = s.step(candles, 30, "test-key", decider=fake_decision("BUY"),
                   now=candles.index[-1].to_pydatetime() + timedelta(minutes=15))
    assert event["fill"] is None
    assert event["reason"] == "spread_limit"
    assert s.state["pending_weight"] is None
    s.close()


def test_low_probability_never_creates_pending_order(candles, tmp_path):
    s = PaperSession(tmp_path / "prob.db", "BTCUSDT", "15m", 80)
    def weak(*args):
        return {"choice": "BUY", "probabilities": {"BUY": 0.5, "SELL": 0.1,
                "HOLD": 0.4}, "confidence": 0.3, "input_tokens": 100,
                "model": "jev-1.13.0"}
    event = s.step(candles, 1, "test-key", decider=weak,
                   now=candles.index[-1].to_pydatetime() + timedelta(minutes=15))
    assert event["reason"] == "low_probability"
    assert s.state["pending_weight"] is None
    s.close()


def test_client_parses_typed_choice_without_network():
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"model": "jev-1.13.0", "usage": {"input_tokens": 320},
                    "answers": {"action": {"type": "choice", "choice": "HOLD",
                    "probabilities": {"BUY": 0.1, "SELL": 0.1, "HOLD": 0.8},
                    "confidence": 0.7}}}
    def post(url, headers, json, timeout):
        assert url.endswith("/v1/systemone")
        assert headers["Authorization"] == "Bearer test-key"
        assert json["questions"]["action"]["type"] == "choice"
        assert timeout == 5
        return Response()
    result = ask_jev({"close": 1.0}, "test-key", post=post)
    assert result["choice"] == "HOLD"
    assert result["input_tokens"] == 320
