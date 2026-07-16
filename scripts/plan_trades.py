#!/usr/bin/env python3
"""Step 2 (evening): filter scan results + size positions -> trade plan JSON.

Reads the newest scan_results/short_candidates_*.csv, applies the config
filters (score, setup, F&O, ban list, ADX, reward:risk, stop distance),
sizes each trade off the risk budget, enforces portfolio caps, and writes
orders/trade_plan_<date>.json for place_entries.py.
"""

import argparse
import dataclasses
import datetime as dt
import json
import os

import pandas as pd

from mr_short.filters import select_candidates
from mr_short.risk import apply_portfolio_limits, size_trade
from mr_short.utils import ORDERS_DIR, SCAN_DIR, get_logger, inr, latest_file, load_config

log = get_logger("plan_trades")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan-file", help="specific scan CSV (default: newest)")
    ap.add_argument("--capital", type=float, help="override config capital")
    args = ap.parse_args()

    cfg = load_config()
    if args.capital:
        cfg["capital"] = args.capital

    scan_path = args.scan_file or latest_file(os.path.join(SCAN_DIR, "short_candidates_*.csv"))
    scan = pd.read_csv(scan_path).fillna({"fno": "", "lot_size": 0})
    log.info(f"scan file: {scan_path} ({len(scan)} candidates)")

    selected, rejections = select_candidates(scan, cfg)
    for sym, why in rejections:
        log.info(f"  reject {sym}: {why}")

    if selected.empty:
        print("\nNo tradeable candidates after filters. No plan written.")
        return

    sized, skips = [], []
    for _, row in selected.iterrows():
        trade, why = size_trade(row, cfg)
        if trade is None:
            skips.append((row["symbol"], why))
        else:
            trade_dict = dataclasses.asdict(trade)
            trade_dict["scan_day_low"] = float(row["day_low"])
            trade_dict["score"] = float(row["score"])
            trade_dict["setup"] = row["setup"]
            sized.append((trade, trade_dict))

    kept, limit_skips = apply_portfolio_limits([t for t, _ in sized], cfg)
    kept_syms = {t.symbol for t in kept}
    plan_trades = [d for t, d in sized if t.symbol in kept_syms]
    skips += limit_skips
    for sym, why in skips:
        log.info(f"  skip {sym}: {why}")

    if not plan_trades:
        print("\nAll candidates were skipped during sizing. No plan written.")
        return

    stamp = dt.date.today().isoformat()
    plan = {
        "date": stamp,
        "scan_file": os.path.basename(scan_path),
        "capital": cfg["capital"],
        "entry_mode": cfg["entry"]["mode"],
        "product": cfg["entry"]["product"],
        "trades": plan_trades,
    }
    os.makedirs(ORDERS_DIR, exist_ok=True)
    out = os.path.join(ORDERS_DIR, f"trade_plan_{stamp}.json")
    with open(out, "w") as f:
        json.dump(plan, f, indent=2)

    total_risk = sum(t["risk_amount"] for t in plan_trades)
    total_notional = sum(t["notional"] for t in plan_trades)
    print(f"\nTRADE PLAN {stamp}  ({cfg['entry']['product']}, entry: {cfg['entry']['mode']})")
    print("-" * 100)
    for t in plan_trades:
        lots = f"{t['lots']} lot(s) x {t['lot_size']}" if t["lots"] else f"{t['qty']} sh"
        print(f"  SHORT {t['symbol']:<12} {lots:<18} trig {t['entry']:>9.2f} "
              f"stop {t['stop']:>9.2f}  target {t['target']:>9.2f}  "
              f"risk {inr(t['risk_amount']):>12}  R:R {t['reward_risk']}")
    print("-" * 100)
    print(f"  trades: {len(plan_trades)}  |  total risk {inr(total_risk)} "
          f"({100 * total_risk / cfg['capital']:.2f}% of capital)  |  "
          f"notional {inr(total_notional)} "
          f"({100 * total_notional / cfg['capital']:.1f}% of capital)")
    print(f"  plan -> {out}\n  next: python scripts/place_entries.py  (dry run; add --live to trade)")


if __name__ == "__main__":
    main()
