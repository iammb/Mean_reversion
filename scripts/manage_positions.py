#!/usr/bin/env python3
"""Step 4 (intraday + daily): manage open trades from the state journal.

For each journaled trade:
  PENDING_ENTRY  - if the entry order filled -> place the exit pair
                   (SL-M stop + LIMIT target) and mark OPEN.
                   If unfilled past entry_valid_sessions -> cancel, CANCELLED.
  OPEN           - OCO: if stop filled -> cancel target (and vice versa),
                   mark CLOSED. Exit orders are DAY validity, so re-place
                   them each morning. Time stop: close at market after
                   time_stop_sessions. MIS trades: square off by the
                   configured time.

Run it a few times a day (or on a cron/loop every few minutes while the
market is open). DRY RUN by default; --live acts on the broker.
--assume-filled treats pending entries as filled (dry-run demo only).
"""

import argparse
import datetime as dt

from mr_short.kite.instruments import Instrument
from mr_short.kite.orders import OrderEngine
from mr_short.utils import get_logger, load_config, load_state, save_state

log = get_logger("manage_positions")


def _inst(t) -> Instrument:
    return Instrument(t["exchange"], t["tradingsymbol"], 1, "", indicative=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="act on the broker")
    ap.add_argument("--assume-filled", action="store_true",
                    help="dry-run demo: treat pending entries as filled")
    args = ap.parse_args()

    cfg = load_config()
    kite = None
    if args.live:
        from mr_short.kite.auth import get_kite
        kite = get_kite(cfg)

    engine = OrderEngine(kite, cfg, dry_run=not args.live)
    state = load_state()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%H:%M")
    time_stop = cfg["exits"]["time_stop_sessions"]
    max_entry_age = cfg["entry"]["entry_valid_sessions"]

    for t in state["trades"]:
        if t["status"] in ("CLOSED", "CANCELLED"):
            continue
        inst = _inst(t)

        # advance session counter once per calendar day
        if t.get("last_managed") != today:
            if t["status"] == "OPEN":
                t["sessions_held"] = t.get("sessions_held", 0) + 1
            t["last_managed"] = today

        if t["status"] == "PENDING_ENTRY":
            status = engine.order_status(t["entry_order_id"])
            filled = status == "COMPLETE" or (args.assume_filled and not args.live)
            if filled:
                sl_id, tgt_id = engine.place_exit_pair(inst, t["qty"], t["stop"], t["target"])
                t.update(status="OPEN", sl_order_id=sl_id, target_order_id=tgt_id)
                log.info(f"{t['symbol']}: entry filled -> exits placed (OPEN)")
            elif status in ("CANCELLED", "REJECTED"):
                t["status"] = "CANCELLED"
                log.info(f"{t['symbol']}: entry {status}")
            else:
                age = (dt.date.fromisoformat(today)
                       - dt.date.fromisoformat(t["entry_date"])).days
                if age >= max_entry_age and now > "15:00":
                    engine.cancel(t["entry_order_id"], "entry validity expired")
                    t["status"] = "CANCELLED"
                    log.info(f"{t['symbol']}: unfilled entry cancelled (bounce never failed)")
                else:
                    log.info(f"{t['symbol']}: entry still pending ({status})")
            continue

        # ---- OPEN position management ----
        sl_status = engine.order_status(t["sl_order_id"]) if t["sl_order_id"] else "NONE"
        tgt_status = engine.order_status(t["target_order_id"]) if t["target_order_id"] else "NONE"

        if sl_status == "COMPLETE":
            engine.cancel(t["target_order_id"], "OCO: stop hit")
            t["status"] = "CLOSED"
            t["exit_reason"] = "STOP"
            log.info(f"{t['symbol']}: STOPPED OUT")
        elif tgt_status == "COMPLETE":
            engine.cancel(t["sl_order_id"], "OCO: target hit")
            t["status"] = "CLOSED"
            t["exit_reason"] = "TARGET"
            log.info(f"{t['symbol']}: TARGET HIT (mean reached)")
        elif t["sessions_held"] >= time_stop:
            engine.cancel(t["sl_order_id"], "time stop")
            engine.cancel(t["target_order_id"], "time stop")
            engine.close_at_market(inst, t["qty"], f"time stop {time_stop} sessions")
            t["status"] = "CLOSED"
            t["exit_reason"] = "TIME_STOP"
        elif t["product"] == "MIS_EQ" and now >= cfg["exits"]["mis_squareoff_time"]:
            engine.cancel(t["sl_order_id"], "EOD square-off")
            engine.cancel(t["target_order_id"], "EOD square-off")
            engine.close_at_market(inst, t["qty"], "MIS EOD square-off")
            t["status"] = "CLOSED"
            t["exit_reason"] = "EOD_SQUAREOFF"
        elif sl_status in ("CANCELLED", "REJECTED", "NONE") and t["status"] == "OPEN":
            # exit orders are DAY validity - re-arm each morning
            sl_id, tgt_id = engine.place_exit_pair(inst, t["qty"], t["stop"], t["target"])
            t.update(sl_order_id=sl_id, target_order_id=tgt_id)
            log.info(f"{t['symbol']}: exit pair re-armed for the day")
        else:
            log.info(f"{t['symbol']}: OPEN day {t['sessions_held']}/{time_stop} "
                     f"(stop {sl_status}, target {tgt_status})")

    save_state(state)
    open_n = sum(1 for t in state["trades"] if t["status"] == "OPEN")
    pend_n = sum(1 for t in state["trades"] if t["status"] == "PENDING_ENTRY")
    print(f"\nstate: {open_n} open, {pend_n} pending entry "
          f"-> orders/positions_state.json")


if __name__ == "__main__":
    main()
