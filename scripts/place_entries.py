#!/usr/bin/env python3
"""Step 3 (pre-open / at open): place entry orders from the trade plan.

DRY RUN by default - prints exactly what would be sent to Kite and journals
the trades in orders/positions_state.json. Pass --live (with Kite keys
configured and today's access token minted) to place real orders.
"""

import argparse
import datetime as dt
import json
import os

from mr_short.kite.instruments import resolve
from mr_short.kite.orders import OrderEngine
from mr_short.risk import daily_loss_breached
from mr_short.utils import (ORDERS_DIR, get_logger, latest_file, load_config,
                            load_state, save_state)

log = get_logger("place_entries")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-file", help="specific plan JSON (default: newest)")
    ap.add_argument("--live", action="store_true", help="place REAL orders")
    args = ap.parse_args()

    cfg = load_config()
    dry = not args.live

    kite = None
    if args.live:
        from mr_short.kite.auth import get_kite
        kite = get_kite(cfg)

    plan_path = args.plan_file or latest_file(os.path.join(ORDERS_DIR, "trade_plan_*.json"))
    with open(plan_path) as f:
        plan = json.load(f)
    log.info(f"plan: {plan_path} ({len(plan['trades'])} trades) "
             f"{'[DRY RUN]' if dry else '[LIVE]'}")

    if daily_loss_breached(kite, cfg):
        return

    engine = OrderEngine(kite, cfg, dry_run=dry)
    state = load_state()
    already = {t["symbol"] for t in state["trades"] if t["status"] in
               ("PENDING_ENTRY", "OPEN")}

    placed = 0
    for t in plan["trades"]:
        if t["symbol"] in already:
            log.info(f"  {t['symbol']}: already pending/open - skipping duplicate")
            continue
        inst = resolve(kite, t["symbol"], t["product"], t.get("lot_size", 0))
        if not engine.check_margin(inst, t["qty"], t["entry"]):
            continue
        entry_id = engine.place_short_entry(inst, t["qty"], t["scan_day_low"])
        state["trades"].append({
            "symbol": t["symbol"],
            "tradingsymbol": inst.tradingsymbol,
            "exchange": inst.exchange,
            "product": t["product"],
            "qty": t["qty"],
            "entry": t["entry"],
            "stop": t["stop"],
            "target": t["target"],
            "risk_amount": t["risk_amount"],
            "entry_order_id": entry_id,
            "sl_order_id": None,
            "target_order_id": None,
            "status": "PENDING_ENTRY",
            "entry_date": dt.date.today().isoformat(),
            "sessions_held": 0,
            "plan_file": os.path.basename(plan_path),
        })
        placed += 1

    save_state(state)
    print(f"\n{placed} entry order(s) {'simulated' if dry else 'PLACED LIVE'} - "
          f"state journal: {os.path.join(ORDERS_DIR, 'positions_state.json')}")
    print("next: python scripts/manage_positions.py  (run intraday + daily)")


if __name__ == "__main__":
    main()
