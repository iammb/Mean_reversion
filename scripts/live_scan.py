#!/usr/bin/env python3
"""Live morning scan + 1-minute entries, 09:15 -> 11:00 IST.

Combines the previous session's EOD setups (the validated dead-cat-bounce
scan) with live intraday data. Stock selection is the priority: the live
stage re-filters and re-ranks the watchlist every few minutes, arms only
names behaving correctly (below VWAP, weak vs Nifty, real volume, near the
trigger), and enters only on a 1-minute close through the entry level.
Everything is done by 11:00 - after that the script summarises and exits.

  python scripts/live_scan.py            # dry run, full morning loop
  python scripts/live_scan.py --once     # single evaluation pass (any time)
  python scripts/live_scan.py --live     # real orders (needs dry_run: false)

Run the EOD scan the previous evening first: scripts/run_scan.py
"""

import argparse
import datetime as dt
import os
import time

import pandas as pd

import math

from mr_short.filters import select_candidates
from mr_short.kite.instruments import resolve
from mr_short.kite.orders import OrderEngine
from mr_short.live.data import get_index_change_pct, get_intraday, now_ist
from mr_short.live.scanner import (Candidate, build_fvg_entry, check_breakdown,
                                   check_entry, evaluate_candidate, rank_table)
from mr_short.risk import daily_loss_breached, size_trade
from mr_short.utils import (SCAN_DIR, get_logger, is_trading_day, latest_file,
                            load_config, load_state, save_state)

log = get_logger("live_scan")


def build_watchlist(cfg) -> list:
    """Previous session's EOD candidates -> sized Candidate objects."""
    scan_path = latest_file(os.path.join(SCAN_DIR, "short_candidates_*.csv"))
    scan_date = os.path.basename(scan_path)[len("short_candidates_"):-len(".csv")]
    age = (dt.date.today() - dt.date.fromisoformat(scan_date)).days
    if age > 3:
        raise SystemExit(f"scan file is {age} days old ({scan_date}) - "
                         "run scripts/run_scan.py after the previous close")
    log.info(f"EOD setups from {scan_path}")

    scan = pd.read_csv(scan_path).fillna({"fno": "", "lot_size": 0})
    selected, rejections = select_candidates(scan, cfg)
    for sym, why in rejections:
        log.info(f"  EOD reject {sym}: {why}")
    if selected.empty:
        return []

    live_syms = {t["symbol"] for t in load_state()["trades"]
                 if t["status"] in ("PENDING_ENTRY", "OPEN")}
    fvg_mode = cfg["entry"]["mode"] == "fvg_retrace"
    out = []
    for _, row in selected.iterrows():
        if row["symbol"] in live_syms:
            log.info(f"  skip {row['symbol']}: already open/pending")
            continue
        trigger = float(row["day_low"]) * (1 - cfg["entry"]["breakdown_buffer_pct"] / 100)
        target = float(row["target_20ema"])
        if fvg_mode:
            # stop/qty are decided at entry time from the FVG zone; the EOD
            # gate is simply "is there enough meat between trigger and target"
            move_pct = 100.0 * (trigger - target) / trigger
            if move_pct < cfg["live"].get("min_reward_move_pct", 1.5):
                log.info(f"  skip {row['symbol']}: only {move_pct:.1f}% to target")
                continue
            qty, lots, lot_size, stop = 0, 0, 0, float(row["stop"])
        else:
            trade, why = size_trade(row, cfg)
            if trade is None:
                log.info(f"  sizing skip {row['symbol']}: {why}")
                continue
            qty, lots, lot_size, stop = trade.qty, trade.lots, trade.lot_size, trade.stop
        out.append(Candidate(
            symbol=row["symbol"], eod_score=float(row["score"]),
            trigger=trigger, stop=stop, target=target, atr=float(row["atr14"]),
            prev_close=float(row["close"]),
            avg_daily_volume=float(row["turnover_cr"]) * 1e7 / float(row["close"]),
            qty=qty, lots=lots, lot_size=lot_size,
        ))
    return out


def size_intraday(entry: float, stop: float, cfg) -> int:
    """MIS share sizing at entry time: fixed-risk on the live stop distance."""
    budget = cfg["capital"] * cfg["risk"]["risk_per_trade_pct"] / 100.0
    per_share = stop - entry
    if per_share <= 0:
        return 0
    qty = math.floor(budget / per_share)
    max_notional = cfg["capital"] * cfg["risk"]["max_exposure_per_trade_pct"] / 100.0
    return max(0, min(qty, math.floor(max_notional / entry)))


def place_entry(c: Candidate, engine: OrderEngine, cfg, kite, state,
                entry_px: float, stop: float, target: float, qty: int,
                style: str) -> None:
    inst = resolve(kite, c.symbol, cfg["entry"]["product"], c.lot_size)
    if qty <= 0 or not engine.check_margin(inst, qty, entry_px):
        c.status, c.drop_reason = "DROPPED", "sizing/margin failed at entry"
        return
    entry_id = engine.place_short_market(inst, qty, f"{style} entry")
    gtt_id = sl_id = tgt_id = None
    if cfg["exits"].get("use_gtt", True) and cfg["entry"]["product"] == "NRML_FUT":
        gtt_id = engine.place_gtt_oco(inst, qty, stop, target, last_price=entry_px)
    else:
        sl_id, tgt_id = engine.place_exit_pair(inst, qty, stop, target)
    state["trades"].append({
        "symbol": c.symbol, "tradingsymbol": inst.tradingsymbol,
        "exchange": inst.exchange, "product": cfg["entry"]["product"],
        "qty": qty, "entry": entry_px, "stop": stop, "target": target,
        "scan_day_low": round(c.trigger / (1 - cfg["entry"]["breakdown_buffer_pct"] / 100), 2),
        "risk_amount": round(qty * (stop - entry_px), 0),
        "entry_order_id": entry_id, "sl_order_id": sl_id,
        "target_order_id": tgt_id, "gtt_id": gtt_id, "status": "OPEN",
        "entry_date": dt.date.today().isoformat(), "sessions_held": 0,
        "plan_file": "live_scan", "entry_mode": style,
    })
    save_state(state)
    c.status = "ENTERED"
    log.info(f"*** ENTERED {c.symbol} x{qty} @ ~{entry_px} ({style}, "
             f"stop {stop:.2f}, target {target:.2f}) ***")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="real orders")
    ap.add_argument("--once", action="store_true",
                    help="one evaluation pass and exit (works after hours)")
    ap.add_argument("--capital", type=float, help="override config capital")
    args = ap.parse_args()

    cfg = load_config()
    if args.capital:
        cfg["capital"] = args.capital
    kite = None
    if args.live:
        if cfg.get("dry_run", True):
            raise SystemExit("--live passed but config.yaml still has dry_run: true.")
        from mr_short.kite.auth import get_kite
        kite = get_kite(cfg)

    if not is_trading_day() and not args.once:
        print("weekend - market closed")
        return

    lcfg = cfg["live"]
    engine = OrderEngine(kite, cfg, dry_run=not args.live)
    state = load_state()
    watch = build_watchlist(cfg)
    if not watch:
        print("no EOD candidates to watch today - no trade day")
        return
    log.info(f"watchlist: {[c.symbol for c in watch]}")

    open_n = sum(1 for t in state["trades"] if t["status"] in ("PENDING_ENTRY", "OPEN"))
    capacity = min(cfg["risk"]["max_new_trades_per_day"],
                   cfg["risk"]["max_positions"] - open_n)
    if capacity <= 0:
        print("no capacity (position limits) - not scanning")
        return

    cutoff = dt.datetime.strptime(lcfg["entry_cutoff"], "%H:%M").time()
    start = dt.datetime.strptime(lcfg["scan_start"], "%H:%M").time()
    tick = lcfg["entry_check_interval_sec"]
    rescan_every = max(1, lcfg["rescan_interval_sec"] // tick)
    index_chg = get_index_change_pct()

    n = 0
    while True:
        now = now_ist()
        active = [c for c in watch if c.status in ("WATCH", "ARMED", "BROKEN")]
        if not active or capacity <= 0:
            log.info("nothing left to watch or no capacity - done")
            break
        if not args.once and now.time() >= cutoff:
            log.info(f"{lcfg['entry_cutoff']} IST cutoff - scanning window over")
            break

        bars = get_intraday([c.symbol for c in active], interval="1m")
        if n % rescan_every == 0:
            index_chg = get_index_change_pct()
        for c in active:
            evaluate_candidate(c, bars.get(c.symbol), index_chg, lcfg)

        if n % rescan_every == 0 or args.once:
            print(f"\n--- {now.strftime('%H:%M:%S')} IST | Nifty {index_chg:+.2f}% "
                  f"| capacity {capacity} ---")
            print(rank_table(watch).to_string(index=False))

        if args.once:
            break
        if now.time() >= start and not daily_loss_breached(kite, cfg):
            fvg_mode = cfg["entry"]["mode"] == "fvg_retrace"
            hot = sorted([c for c in active if c.status in ("ARMED", "BROKEN")],
                         key=lambda c: -c.live.get("score", 0))
            for c in hot:
                if capacity <= 0:
                    break
                b = bars.get(c.symbol)
                if fvg_mode:
                    if c.status == "ARMED":
                        check_breakdown(c, b, lcfg)   # phase 1: arm the hunt
                    if c.status == "BROKEN":
                        sig = build_fvg_entry(c, b, lcfg)  # phase 2: retrace
                        if sig:
                            qty = size_intraday(sig["entry"], sig["stop"], cfg)
                            place_entry(c, engine, cfg, kite, state,
                                        sig["entry"], sig["stop"], sig["target"],
                                        qty, sig["style"])
                elif check_entry(c, b, lcfg):
                    qty = c.qty or size_intraday(c.live.get("px", c.entry_level),
                                                 c.stop, cfg)
                    place_entry(c, engine, cfg, kite, state,
                                c.live.get("px", c.entry_level), c.stop,
                                c.target, qty, "1m_breakdown")
                if c.status == "ENTERED":
                    capacity -= 1
        n += 1
        time.sleep(tick)

    entered = [c.symbol for c in watch if c.status == "ENTERED"]
    print(f"\nsession done: entered {entered or 'nothing'} | "
          f"next: scripts/manage_positions.py handles exits")


if __name__ == "__main__":
    main()
