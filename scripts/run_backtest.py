#!/usr/bin/env python3
"""Backtest the short-side mean reversion strategy over N years of history.

Downloads Nifty 500 daily data (cached), replays the exact scanner +
trade-plan rules, and reports per-trade statistics plus a portfolio
simulation honouring the live risk limits.

  python scripts/run_backtest.py               # 5-year backtest, F&O names
  python scripts/run_backtest.py --years 3
  python scripts/run_backtest.py --all-stocks  # ignore the F&O filter
"""

import argparse
import datetime as dt
import os

import pandas as pd

from mr_short.backtest import (compute_signals, download_history,
                               portfolio_sim, simulate_symbol, summarize)
from mr_short.universe import load_fno_lots, load_universe
from mr_short.utils import ROOT, get_logger, inr

log = get_logger("run_backtest")
OUT_DIR = os.path.join(ROOT, "backtest_results")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=5, help="backtest window (default 5)")
    ap.add_argument("--all-stocks", action="store_true",
                    help="include non-F&O names (cash-short territory)")
    ap.add_argument("--fresh", action="store_true", help="ignore the price cache")
    # strategy geometry - defaults are the LIVE (v2) settings
    ap.add_argument("--setups", nargs="+", default=["DEAD-CAT BOUNCE"],
                    help='default: DEAD-CAT BOUNCE only (v1: add "BLOWOFF EXTENSION")')
    ap.add_argument("--stop-atr", type=float, default=1.0,
                    help="stop = swing high + this many ATRs (v1: 0.25)")
    ap.add_argument("--min-rr", type=float, default=1.2, help="v1: 2.0")
    ap.add_argument("--max-stop-dist", type=float, default=0.06, help="v1: 0.035")
    ap.add_argument("--time-stop", type=int, default=7)
    args = ap.parse_args()

    universe = load_universe()
    fno = set(load_fno_lots())
    symbols = universe["symbol"].tolist()
    if not args.all_stocks:
        symbols = [s for s in symbols if s in fno]
    log.info(f"backtest universe: {len(symbols)} symbols "
             f"({'all Nifty 500' if args.all_stocks else 'F&O only'})")

    prices = download_history(universe["symbol"].tolist(),
                              years=args.years + 1, use_cache=not args.fresh)
    start = dt.date.today() - dt.timedelta(days=int(365.25 * args.years))

    all_trades = []
    for sym in symbols:
        df = prices.get(sym)
        if df is None:
            continue
        sig = compute_signals(df, stop_atr=args.stop_atr, min_rr=args.min_rr,
                              max_stop_dist=args.max_stop_dist,
                              setups=tuple(args.setups))
        all_trades += simulate_symbol(sym, df, sig, start, time_stop=args.time_stop)

    if not all_trades:
        print("No trades generated - nothing to report.")
        return

    trades = pd.DataFrame(all_trades)
    resolved = trades[trades["exit_reason"] != "OPEN_EOD"].copy()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = f"{args.years}y_{dt.date.today().isoformat()}"
    trades_path = os.path.join(OUT_DIR, f"trades_{stamp}.csv")
    trades.to_csv(trades_path, index=False)

    # ---------------- per-trade statistics ----------------
    lines = []
    p = lines.append
    p(f"BACKTEST  {start} -> {dt.date.today()}  "
      f"({'all Nifty 500' if args.all_stocks else 'F&O universe'}, "
      f"costs 0.05%/side)")
    p("=" * 78)
    o = summarize(resolved)
    p(f"signals filled: {len(resolved)} trades   "
      f"win rate {o['win_rate_pct']}%   profit factor {o['profit_factor']}")
    p(f"expectancy {o['expectancy_pct']:+.3f}%/trade   avg R {o['avg_r']:+.3f}   "
      f"avg win {o['avg_win_pct']:+.2f}%   avg loss {o['avg_loss_pct']:+.2f}%   "
      f"median hold {o['median_days_held']}d")

    p("\nBy setup:")
    for s, grp in resolved.groupby("setup"):
        g = summarize(grp)
        p(f"  {s:<18} {g['trades']:>4} trades  win {g['win_rate_pct']:>5}%  "
          f"exp {g['expectancy_pct']:+.3f}%  PF {g['profit_factor']:>5}  "
          f"avgR {g['avg_r']:+.3f}")

    p("\nBy exit reason:")
    for s, grp in resolved.groupby("exit_reason"):
        p(f"  {s:<12} {len(grp):>4} trades ({100 * len(grp) / len(resolved):.0f}%)  "
          f"avg {grp['ret_pct'].mean():+.2f}%")

    p("\nBy year (signal date):")
    resolved["year"] = pd.to_datetime(resolved["signal_date"]).dt.year
    for y, grp in resolved.groupby("year"):
        g = summarize(grp)
        p(f"  {y}  {g['trades']:>4} trades  win {g['win_rate_pct']:>5}%  "
          f"exp {g['expectancy_pct']:+.3f}%  PF {g['profit_factor']}")

    # ---------------- portfolio simulation ----------------
    taken, curve = portfolio_sim(resolved)
    if not taken.empty:
        eq0, eq1 = 5_000_000, taken["equity"].iloc[-1]
        years_frac = max((taken["exit_date"].iloc[-1]
                          - taken["entry_date"].iloc[0]).days / 365.25, 0.1)
        cagr = 100 * ((eq1 / eq0) ** (1 / years_frac) - 1)
        peak = curve["equity"].cummax()
        max_dd = 100 * ((curve["equity"] - peak) / peak).min()
        p("\nPortfolio simulation (Rs 50L, 0.75% risk/trade, max 5 open, "
          "3/day, exposure caps):")
        p(f"  trades taken: {len(taken)} of {len(resolved)} signals")
        p(f"  final equity: {inr(eq1)}  ({100 * (eq1 - eq0) / eq0:+.1f}% total, "
          f"{cagr:+.1f}% CAGR over {years_frac:.1f}y)")
        p(f"  max drawdown: {max_dd:.1f}%   "
          f"best trade {inr(taken['pnl'].max())}   worst {inr(taken['pnl'].min())}")
        taken.to_csv(os.path.join(OUT_DIR, f"portfolio_{stamp}.csv"), index=False)
        curve.to_csv(os.path.join(OUT_DIR, f"equity_curve_{stamp}.csv"))

    p("\nCaveats: today's index membership & F&O list applied to the past "
      "(survivorship bias); equity prices proxy futures; no ban-list or "
      "earnings avoidance; fractional sizing (no lot rounding).")
    p(f"trades -> {trades_path}")

    report = "\n".join(lines)
    with open(os.path.join(OUT_DIR, f"summary_{stamp}.txt"), "w") as f:
        f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
