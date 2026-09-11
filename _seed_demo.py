"""Seed synthetic candles under the symbol DEMOUSDT so the engine can be
tested offline. Deliberately NOT stored as BTCUSDT — this is fake data and
must never mix with real cached candles."""
import numpy as np, pandas as pd, sqlite3, feed
rng = np.random.default_rng(7)
n = 4000
# regime-switching drift + vol, so it isn't a clean trend
drift = np.concatenate([np.full(1200,  0.00004), np.full(900, -0.00006),
                        np.full(1100,  0.00002), np.full(800,  0.00007)])
vol   = np.concatenate([np.full(1200,  0.00090), np.full(900,  0.00180),
                        np.full(1100,  0.00060), np.full(800,  0.00120)])
r = drift + vol * rng.standard_normal(n)
close = 68000 * np.exp(np.cumsum(r))
op = np.concatenate([[68000], close[:-1]])
wig = vol * close * rng.uniform(0.5, 2.2, n)
high = np.maximum(op, close) + wig
low  = np.minimum(op, close) - wig
volu = rng.lognormal(2.4, 0.7, n) * (1 + 40*np.abs(r))
start = pd.Timestamp("2026-09-07 00:00", tz="UTC")
ms = int(start.timestamp()*1000) + np.arange(n)*60000
conn = feed._db()
conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)",
    [("DEMOUSDT","1m",int(ms[i]),float(op[i]),float(high[i]),float(low[i]),
      float(close[i]),float(volu[i]),int(volu[i]*7)) for i in range(n)])
conn.commit(); conn.close()
print(f"seeded {n} synthetic candles  {close[0]:,.0f} -> {close[-1]:,.0f}")
