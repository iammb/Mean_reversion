#!/usr/bin/env python3
"""Compare strategy variants over the same history to validate corrections.

Runs the baseline and each candidate fix against the cached 5-year data and
prints a side-by-side table, so changes to the live strategy are driven by
evidence instead of vibes.
"""

import datetime as dt

import pandas as pd

from mr_short.backtest import (compute_signals, download_history, get_regime,
                               portfolio_sim, simulate_symbol, summarize)
from mr_short.universe import load_fno_lots, load_universe
from mr_short.utils import get_logger

log = get_logger("compare_variants")

YEARS = 5
DCB = ("DEAD-CAT BOUNCE",)
BOTH = ("DEAD-CAT BOUNCE", "BLOWOFF EXTENSION")

VARIANTS = {
    "v1 baseline (both setups)": dict(setups=BOTH, stop_atr=0.25, min_rr=2.0,
                                      max_stop_dist=0.035, time_stop=7, regime=False),
    "v2a DCB only":              dict(setups=DCB, stop_atr=0.25, min_rr=2.0,
                                      max_stop_dist=0.035, time_stop=7, regime=False),
    "v2b DCB + wide stop":       dict(setups=DCB, stop_atr=1.0, min_rr=1.2,
                                      max_stop_dist=0.06, time_stop=7, regime=False),
    "v2c DCB + wide + regime":   dict(setups=DCB, stop_atr=1.0, min_rr=1.2,
                                      max_stop_dist=0.06, time_stop=7, regime=True),
    "v2d v2c + 10d time stop":   dict(setups=DCB, stop_atr=1.0, min_rr=1.2,
                                      max_stop_dist=0.06, time_stop=10, regime=True),
    "v2e both + wide + regime":  dict(setups=BOTH, stop_atr=1.0, min_rr=1.2,
                                      max_stop_dist=0.06, time_stop=7, regime=True),
}


def main():
    universe = load_universe()
    fno = set(load_fno_lots())
    symbols = [s for s in universe["symbol"].tolist() if s in fno]
    prices = download_history(universe["symbol"].tolist(), years=YEARS + 1)
    regime_series = get_regime(years=YEARS + 1, sma=50)
    start = dt.date.today() - dt.timedelta(days=int(365.25 * YEARS))
    log.info(f"{len(symbols)} F&O symbols | regime days below 50-DMA: "
             f"{int(regime_series.sum())}/{len(regime_series)}")

    rows = []
    for name, v in VARIANTS.items():
        trades = []
        reg = regime_series if v["regime"] else None
        for sym in symbols:
            df = prices.get(sym)
            if df is None:
                continue
            sig = compute_signals(df, stop_atr=v["stop_atr"], min_rr=v["min_rr"],
                                  max_stop_dist=v["max_stop_dist"], setups=v["setups"])
            trades += simulate_symbol(sym, df, sig, start,
                                      time_stop=v["time_stop"], regime=reg)
        tdf = pd.DataFrame(trades)
        if tdf.empty:
            rows.append({"variant": name, "trades": 0})
            continue
        tdf = tdf[tdf["exit_reason"] != "OPEN_EOD"]
        s = summarize(tdf)
        taken, curve = portfolio_sim(tdf)
        eq1 = taken["equity"].iloc[-1] if not taken.empty else 5_000_000
        peak = curve["equity"].cummax()
        max_dd = 100 * ((curve["equity"] - peak) / peak).min() if not curve.empty else 0
        rows.append({
            "variant": name, "trades": s["trades"], "win%": s["win_rate_pct"],
            "exp%/tr": s["expectancy_pct"], "PF": s["profit_factor"],
            "avgR": s["avg_r"], "port_ret%": round(100 * (eq1 - 5e6) / 5e6, 1),
            "maxDD%": round(max_dd, 1),
        })
        log.info(f"done: {name}")

    out = pd.DataFrame(rows)
    print("\n" + out.to_string(index=False))


if __name__ == "__main__":
    main()
