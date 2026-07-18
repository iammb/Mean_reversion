"""Live morning scan: previous-session EOD setups filtered and ranked by
live intraday behaviour, producing ARMED candidates for 1-minute entries.

The funnel (stock selection is the whole game - filter hard at every stage):

  EOD stage (validated v2 strategy, previous session's close):
    dead-cat bounce setup, score >= 50, F&O, ban list, ADX, R:R, sizing
  Live stage (every rescan during 09:15-11:00 IST):
    DROP   - gap-up >= 2% (bounce still running), reward gone (price already
             fell past 1/3 of the way to target with < 1.0 R:R left)
    WATCH  - viable but price too far from the trigger
    ARMED  - within 0.5 ATR of the trigger (or gapped through it with an
             opening-range re-anchor): eligible for 1-minute entry checks
  Ranking - EOD score + live points: below VWAP, RVOL, relative weakness
            vs Nifty, below opening-range low, proximity to trigger.
"""

from dataclasses import dataclass, field

import pandas as pd

from ..utils import get_logger
from .data import market_minutes_elapsed
from .intraday import (classify_gap, entry_confirmed, live_score,
                       opening_range, remaining_reward_risk, rvol,
                       session_vwap)

log = get_logger("live.scanner")


@dataclass
class Candidate:
    """One watchlist name: EOD levels + live state."""
    symbol: str
    eod_score: float
    trigger: float          # entry level: 0.1% below previous session's low
    stop: float
    target: float
    atr: float
    prev_close: float
    avg_daily_volume: float
    qty: int
    lots: int
    lot_size: int
    # live state
    status: str = "WATCH"   # WATCH | ARMED | BROKEN | DROPPED | ENTERED
    drop_reason: str = ""
    entry_level: float = 0.0   # trigger, possibly re-anchored on gap-through
    break_idx: int = -1        # completed-bar index of the confirmed breakdown
    live: dict = field(default_factory=dict)


def evaluate_candidate(c: Candidate, bars: pd.DataFrame,
                       index_chg_pct: float, cfg_live: dict) -> None:
    """Refresh one candidate's live metrics and status in place."""
    if c.status in ("DROPPED", "ENTERED"):
        return
    if bars is None or bars.empty:
        c.live = {"note": "no live data"}
        return

    if c.status == "BROKEN":
        # reclaim check: a completed close back ABOVE the broken level means
        # the breakdown failed - go back to ARMED and wait for a fresh break
        done = bars.iloc[:-1]
        if len(done) and float(done["Close"].iloc[-1]) > c.entry_level:
            c.status, c.break_idx = "ARMED", -1
            log.info(f"{c.symbol}: level reclaimed - breakdown cancelled")
        return

    minutes = market_minutes_elapsed()
    px = float(bars["Close"].dropna().iloc[-1])
    open_px = float(bars["Open"].dropna().iloc[0])
    vwap = session_vwap(bars)
    rv = rvol(bars, c.avg_daily_volume, minutes)
    or_high, or_low = opening_range(bars, cfg_live.get("opening_range_min", 15))
    stock_chg = 100.0 * (px - c.prev_close) / c.prev_close
    rel = stock_chg - index_chg_pct
    gap = classify_gap(open_px, c.prev_close, c.trigger)

    c.entry_level = c.trigger
    if gap == "UP_HARD":
        c.status, c.drop_reason = "DROPPED", f"gapped up {open_px / c.prev_close - 1:+.1%} - bounce still running"
        return
    if gap == "THROUGH":
        # opened below the trigger: re-anchor entry to the opening-range low
        # so we only chase if the breakdown CONTINUES
        c.entry_level = or_low

    rr_left = remaining_reward_risk(px, c.stop, c.target)
    fallen_frac = (c.trigger - px) / max(c.trigger - c.target, 1e-9)
    if fallen_frac > 0.33 and rr_left < 1.0:
        c.status, c.drop_reason = "DROPPED", f"move gone (R:R left {rr_left:.2f})"
        return

    dist_atr = abs(px - c.entry_level) / c.atr if c.atr > 0 else 9.9
    score = live_score(c.eod_score, below_vwap=px < vwap, rvol_val=rv,
                       rel_strength=rel, below_or_low=px < or_low,
                       dist_to_trigger_atr=dist_atr)

    c.status = "ARMED" if dist_atr <= cfg_live.get("arm_distance_atr", 0.5) else "WATCH"
    c.live = {
        "px": round(px, 2), "vwap": round(vwap, 2), "below_vwap": px < vwap,
        "rvol": round(rv, 2), "rel_str": round(rel, 2), "gap": gap,
        "or_low": round(or_low, 2), "dist_atr": round(dist_atr, 2),
        "rr_left": round(rr_left, 2), "score": round(score, 1),
        "entry_level": round(c.entry_level, 2),
    }


def check_entry(c: Candidate, bars: pd.DataFrame, cfg_live: dict) -> bool:
    """1-minute confirmation against the (possibly re-anchored) entry level."""
    if c.status != "ARMED" or bars is None or bars.empty:
        return False
    vwap = session_vwap(bars)
    rv = rvol(bars, c.avg_daily_volume, market_minutes_elapsed())
    return entry_confirmed(bars, c.entry_level, vwap, rv,
                           cfg_live.get("rvol_min", 0.8))


def check_breakdown(c: Candidate, bars: pd.DataFrame, cfg_live: dict) -> bool:
    """FVG mode phase 1: the 1-minute breakdown does NOT enter - it starts
    the retrace hunt. Marks the candidate BROKEN and remembers the bar."""
    if not check_entry(c, bars, cfg_live):
        return False
    c.status = "BROKEN"
    c.break_idx = len(bars) - 2          # the completed bar that confirmed
    log.info(f"{c.symbol}: breakdown confirmed below {c.entry_level:.2f} - "
             f"hunting FVG/IFVG retrace entry")
    return True


def build_fvg_entry(c: Candidate, bars: pd.DataFrame, cfg_live: dict):
    """FVG mode phase 2: look for the retrace-rejection short. Returns
    {entry, stop, target, style, rr} or None."""
    from .fvg import find_short_entry

    if c.status != "BROKEN" or bars is None or bars.empty:
        return None
    vwap = session_vwap(bars)
    sig = find_short_entry(bars, c.entry_level, c.break_idx, vwap,
                           cfg_live.get("fvg_min_gap_pct", 0.05),
                           cfg_live.get("zone_buffer_pct", 0.05))
    if not sig:
        return None
    entry, stop, style = sig
    risk = stop - entry
    if risk <= 0:
        return None
    # target: the daily 20-EMA, or the R-multiple extension if that's nearer
    target = max(c.target, entry - cfg_live.get("intraday_target_r", 2.5) * risk)
    rr = (entry - target) / risk
    if rr < cfg_live.get("min_entry_rr", 1.5):
        log.info(f"{c.symbol}: {style} retest found but R:R only {rr:.2f} - pass")
        return None
    return {"entry": entry, "stop": stop, "target": round(target, 2),
            "style": style, "rr": round(rr, 2)}


def rank_table(cands: list) -> pd.DataFrame:
    rows = []
    for c in cands:
        row = {"symbol": c.symbol, "status": c.status, "eod": c.eod_score,
               "entry_lvl": round(c.entry_level, 2), "stop": c.stop,
               "target": c.target}
        row.update(c.live if c.status != "DROPPED" else {"note": c.drop_reason})
        rows.append(row)
    df = pd.DataFrame(rows)
    if "score" in df.columns:
        df = df.sort_values(["status", "score"], ascending=[True, False])
    return df
