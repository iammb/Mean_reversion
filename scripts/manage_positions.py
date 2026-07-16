#!/usr/bin/env python3
"""Step 4 (intraday + daily): manage open trades from the state journal.

For each journaled trade:
  PENDING_ENTRY - if the entry order filled -> place exits and mark OPEN.
                    NRML futures + exits.use_gtt: a server-side GTT OCO
                    (stop above / target below, broker cancels the sibling).
                    MIS equity: regular SL-M stop + LIMIT target pair.
                  If the DAY entry order expired unfilled -> re-arm it next
                  session, up to entry.entry_valid_sessions; then CANCELLED.
  OPEN (GTT)    - triggered -> verify the position is flat, record STOP or
                  TARGET; if the GTT's limit leg didn't fill, fall back to a
                  regular exit pair and shout. Time stop after
                  time_stop_sessions: delete GTT, close at market.
  OPEN (pair)   - client-side OCO: one leg filled -> cancel the sibling;
                  re-arm expired DAY legs each morning; time stop; MIS
                  square-off by the configured time.

Session counters only advance on weekdays; weekend runs are no-ops.
Finished trades are moved to orders/trades_archive.json.

Run every few minutes while the market is open (MIS needs this; GTT trades
only strictly need the morning and time-stop checks). DRY RUN by default;
--live requires config dry_run: false. --assume-filled is a dry-run demo
flag that treats pending entries as filled.
"""

import argparse
import datetime as dt

from mr_short.kite.instruments import Instrument
from mr_short.kite.orders import OrderEngine
from mr_short.utils import (archive_finished_trades, get_logger, is_trading_day,
                            load_config, load_state, save_state, weekday_sessions)

log = get_logger("manage_positions")


def _inst(t) -> Instrument:
    return Instrument(t["exchange"], t["tradingsymbol"], 1, "", indicative=False)


def _handle_pending_entry(t, engine, cfg, args, today):
    inst = _inst(t)
    status = engine.order_status(t["entry_order_id"])
    filled = status == "COMPLETE" or (args.assume_filled and not args.live)

    if filled:
        use_gtt = cfg["exits"].get("use_gtt", True) and t["product"] == "NRML_FUT"
        if use_gtt:
            last = engine.ltp(inst, fallback=t["entry"])
            t["gtt_id"] = engine.place_gtt_oco(inst, t["qty"], t["stop"],
                                               t["target"], last_price=last)
        else:
            sl_id, tgt_id = engine.place_exit_pair(inst, t["qty"], t["stop"], t["target"])
            t.update(sl_order_id=sl_id, target_order_id=tgt_id)
        t["status"] = "OPEN"
        log.info(f"{t['symbol']}: entry filled -> exits placed "
                 f"({'GTT OCO' if use_gtt else 'order pair'})")
        return

    if status == "REJECTED":
        t["status"] = "CANCELLED"
        log.error(f"{t['symbol']}: entry REJECTED by broker - investigate")
        return

    if status == "CANCELLED":  # DAY order expired unfilled
        age = weekday_sessions(dt.date.fromisoformat(t["entry_date"]), today)
        if age < cfg["entry"]["entry_valid_sessions"]:
            t["entry_order_id"] = engine.place_short_entry(inst, t["qty"],
                                                           t["scan_day_low"])
            log.info(f"{t['symbol']}: entry re-armed (session {age + 1} of "
                     f"{cfg['entry']['entry_valid_sessions']})")
        else:
            t["status"] = "CANCELLED"
            log.info(f"{t['symbol']}: entry expired unfilled - bounce never failed")
        return

    log.info(f"{t['symbol']}: entry still pending ({status})")


def _handle_open_gtt(t, engine, cfg, time_stop):
    inst = _inst(t)
    status = engine.gtt_status(t["gtt_id"])

    if status == "triggered":
        leg = engine.gtt_triggered_leg(t["gtt_id"])
        if engine.net_position_qty(inst) < 0:
            # the GTT's LIMIT leg fired but didn't fill - position unprotected
            log.error(f"{t['symbol']}: GTT {leg} triggered but position still "
                      f"short - placing regular exit pair NOW")
            sl_id, tgt_id = engine.place_exit_pair(inst, t["qty"], t["stop"], t["target"])
            t.update(sl_order_id=sl_id, target_order_id=tgt_id, gtt_id=None)
            return
        t.update(status="CLOSED", exit_reason=leg)
        log.info(f"{t['symbol']}: {leg} hit (GTT)")
    elif t["sessions_held"] >= time_stop:
        engine.delete_gtt(t["gtt_id"], "time stop")
        engine.close_at_market(inst, t["qty"], f"time stop {time_stop} sessions")
        t.update(status="CLOSED", exit_reason="TIME_STOP")
    elif status in ("cancelled", "deleted", "expired", "disabled", "rejected"):
        log.warning(f"{t['symbol']}: GTT {status} unexpectedly - re-arming")
        last = engine.ltp(inst, fallback=t["entry"])
        t["gtt_id"] = engine.place_gtt_oco(inst, t["qty"], t["stop"],
                                           t["target"], last_price=last)
    else:
        log.info(f"{t['symbol']}: OPEN day {t['sessions_held']}/{time_stop} (GTT {status})")


def _handle_open_pair(t, engine, cfg, time_stop, now):
    inst = _inst(t)
    sl_status = engine.order_status(t["sl_order_id"]) if t["sl_order_id"] else "NONE"
    tgt_status = engine.order_status(t["target_order_id"]) if t["target_order_id"] else "NONE"

    if sl_status == "COMPLETE":
        engine.cancel(t["target_order_id"], "OCO: stop hit")
        t.update(status="CLOSED", exit_reason="STOP")
        log.info(f"{t['symbol']}: STOPPED OUT")
    elif tgt_status == "COMPLETE":
        engine.cancel(t["sl_order_id"], "OCO: target hit")
        t.update(status="CLOSED", exit_reason="TARGET")
        log.info(f"{t['symbol']}: TARGET HIT (mean reached)")
    elif t["sessions_held"] >= time_stop:
        engine.cancel(t["sl_order_id"], "time stop")
        engine.cancel(t["target_order_id"], "time stop")
        engine.close_at_market(inst, t["qty"], f"time stop {time_stop} sessions")
        t.update(status="CLOSED", exit_reason="TIME_STOP")
    elif t["product"] == "MIS_EQ" and now >= cfg["exits"]["mis_squareoff_time"]:
        engine.cancel(t["sl_order_id"], "EOD square-off")
        engine.cancel(t["target_order_id"], "EOD square-off")
        engine.close_at_market(inst, t["qty"], "MIS EOD square-off")
        t.update(status="CLOSED", exit_reason="EOD_SQUAREOFF")
    elif sl_status in ("CANCELLED", "REJECTED", "NONE"):
        # exit orders are DAY validity - re-arm each morning
        sl_id, tgt_id = engine.place_exit_pair(inst, t["qty"], t["stop"], t["target"])
        t.update(sl_order_id=sl_id, target_order_id=tgt_id)
        log.info(f"{t['symbol']}: exit pair re-armed for the day")
    else:
        log.info(f"{t['symbol']}: OPEN day {t['sessions_held']}/{time_stop} "
                 f"(stop {sl_status}, target {tgt_status})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="act on the broker")
    ap.add_argument("--assume-filled", action="store_true",
                    help="dry-run demo: treat pending entries as filled")
    args = ap.parse_args()

    cfg = load_config()
    kite = None
    if args.live:
        if cfg.get("dry_run", True):
            raise SystemExit("--live passed but config.yaml still has dry_run: true. "
                             "Flip it to false to confirm you mean it.")
        from mr_short.kite.auth import get_kite
        kite = get_kite(cfg)

    today = dt.date.today()
    if not is_trading_day(today):
        print("weekend - market closed, nothing to manage")
        return

    engine = OrderEngine(kite, cfg, dry_run=not args.live)
    state = load_state()
    now = dt.datetime.now().strftime("%H:%M")
    time_stop = cfg["exits"]["time_stop_sessions"]

    for t in state["trades"]:
        if t["status"] in ("CLOSED", "CANCELLED"):
            continue

        # advance the session counter once per trading day
        if t.get("last_managed") != today.isoformat():
            if t["status"] == "OPEN":
                t["sessions_held"] = t.get("sessions_held", 0) + 1
            t["last_managed"] = today.isoformat()

        if t["status"] == "PENDING_ENTRY":
            _handle_pending_entry(t, engine, cfg, args, today)
        elif t.get("gtt_id"):
            _handle_open_gtt(t, engine, cfg, time_stop)
        else:
            _handle_open_pair(t, engine, cfg, time_stop, now)

    archived = archive_finished_trades(state)
    save_state(state)
    open_n = sum(1 for t in state["trades"] if t["status"] == "OPEN")
    pend_n = sum(1 for t in state["trades"] if t["status"] == "PENDING_ENTRY")
    print(f"\nstate: {open_n} open, {pend_n} pending entry"
          + (f", {archived} archived" if archived else "")
          + " -> orders/positions_state.json")


if __name__ == "__main__":
    main()
