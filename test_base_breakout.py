"""
Offline tests for base_breakout_scanner.evaluate() -- synthetic price data, no network.
Builds a textbook setup (run, 20-day base on fading volume, breakout on a volume spike),
confirms it fires, then breaks one rule at a time and confirms each break is caught.

    python test_base_breakout.py
"""
import sys
import numpy as np, pandas as pd, copy
import base_breakout_scanner as S

def make(base_days=20, vol_decline=True, spike=3.0, run_mult=1.55, adr_half=0.035,
        breakout=True, flat_run=False, base_range=(14.6, 15.4)):
    rng = np.random.default_rng(1)
    closes, vols = [], []
    # 45 bars quiet around 10 (older history)
    for i in range(80):
        closes.append(10 + 0.15*np.sin(i/3)); vols.append(600_000 + rng.integers(-50_000, 50_000))
    # run-up 10 -> 15 over 25 bars, heavy volume
    top = 10*run_mult if not flat_run else 10.2
    if flat_run: base_range=(9.9,10.5)
    for i in range(25):
        closes.append(10 + (top-10)*(i+1)/25); vols.append(1_600_000 + rng.integers(-100_000, 100_000))
    # base: oscillate inside range, volume declines
    lo, hi = base_range
    for i in range(base_days):
        closes.append((lo+hi)/2 + (hi-lo)/2*np.sin(i*1.3)*0.9)
        vols.append(int(950_000 - (600_000 * i/(base_days-1))) if vol_decline else 900_000)
    # breakout day
    bo = hi*1.06 if breakout else hi
    closes.append(bo); base_avg = 800_000
    vols.append(int(spike*1_000_000))
    c = np.array(closes)
    h = c*(1+adr_half); l = c*(1-adr_half); o = np.r_[c[0], c[:-1]]
    idx = pd.bdate_range(end="2026-09-18", periods=len(c))
    df = pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=vols), index=idx)
    # make breakout-day high/low sane
    return df

sector_map = {"Technology": dict(sector="Technology", rank=1, strong=True, ret_3m=14.0, above_50dma=True)}
def run(df, mcap=1.2e9, sector="Technology", cfg=None):
    return S.evaluate(df, mcap, sector, sector_map, cfg or S.CFG)

def show(name, ev, expect):
    ok = (ev["stage"] == expect)
    fails = [k for k,v in ev["checks"].items() if not v]
    print(f"{'OK ' if ok else 'BAD'} {name:38s} stage={ev['stage']!s:20s} fails={fails}")
    return ok


results = []
df = make()
results.append(show("textbook setup fires", run(df), "BREAKOUT"))
results.append(show("market cap too big", run(df, mcap=2.6e9), None))
results.append(show("market cap too small", run(df, mcap=3e8), None))
results.append(show("sector not in strong list", run(df, sector="Utilities"), None))
sm = copy.deepcopy(sector_map); sm["Technology"]["strong"] = False
results.append(show("sector not strong", S.evaluate(df, 1.2e9, "Technology", sm), None))
results.append(show("volatility too low", run(make(adr_half=0.010)), None))
results.append(show("volume not declining in base", run(make(vol_decline=False)), None))
results.append(show("no volume spike", run(make(spike=1.2)), "LOW_VOLUME_BREAKOUT"))
results.append(show("still inside base (setup)", run(make(breakout=False)), "SETUP"))
results.append(show("no big prior run", run(make(flat_run=True)), None))
results.append(show("base too deep", run(make(base_range=(11.5, 15.4))), None))
results.append(show("10-day base still valid", run(make(base_days=10)), "BREAKOUT"))
results.append(show("under 100 bars of history", run(df.iloc[-99:]), None))
print("\nALL PASSED" if all(results) else "\nFAILURES")
sys.exit(0 if all(results) else 1)
