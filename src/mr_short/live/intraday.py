"""Pure intraday computations for the live filter pipeline.

Everything here is a function of (intraday bars, EOD levels) -> numbers,
with no I/O, so it is unit-testable and provider-agnostic.
"""

import numpy as np
import pandas as pd

# expected fraction of a full session's volume by minutes elapsed -
# NSE volume is front-loaded; piecewise-linear approximation
_VOL_CURVE = [(0, 0.0), (15, 0.12), (30, 0.18), (60, 0.28),
              (120, 0.45), (195, 0.60), (285, 0.78), (375, 1.0)]


def session_vwap(bars: pd.DataFrame) -> float:
    """Volume-weighted average price over today's bars."""
    tp = (bars["High"] + bars["Low"] + bars["Close"]) / 3.0
    v = bars["Volume"].astype(float)
    if v.sum() <= 0:
        return float(bars["Close"].iloc[-1])
    return float((tp * v).sum() / v.sum())


def expected_volume_fraction(minutes: float) -> float:
    xs, ys = zip(*_VOL_CURVE)
    return float(np.interp(minutes, xs, ys))


def rvol(bars: pd.DataFrame, avg_daily_volume: float, minutes: float) -> float:
    """Relative volume: today's cumulative volume vs the time-adjusted
    20-day average. >1 means heavier than usual for this hour."""
    frac = expected_volume_fraction(minutes)
    if avg_daily_volume <= 0 or frac <= 0:
        return 0.0
    return float(bars["Volume"].sum() / (avg_daily_volume * frac))


def opening_range(bars: pd.DataFrame, minutes: int = 15):
    """(high, low) of the first `minutes` of the session."""
    if bars.empty:
        return np.nan, np.nan
    start = bars.index[0]
    orb = bars[bars.index < start + pd.Timedelta(minutes=minutes)]
    if orb.empty:
        orb = bars.iloc[:1]
    return float(orb["High"].max()), float(orb["Low"].min())


def classify_gap(open_px: float, prev_close: float, trigger: float) -> str:
    """UP_HARD (bounce continuing), THROUGH (opened below trigger), NORMAL."""
    gap_pct = 100.0 * (open_px - prev_close) / prev_close
    if gap_pct >= 2.0:
        return "UP_HARD"
    if open_px <= trigger:
        return "THROUGH"
    return "NORMAL"


def live_score(eod_score: float, *, below_vwap: bool, rvol_val: float,
               rel_strength: float, below_or_low: bool,
               dist_to_trigger_atr: float) -> float:
    """EOD score plus live-behaviour points; ranks the watchlist."""
    s = eod_score
    if below_vwap:
        s += 8
    if rvol_val >= 1.0:
        s += 4
    if rvol_val >= 1.5:
        s += 4
    if rel_strength <= 0:
        s += 4
    if rel_strength <= -1.0:
        s += 4
    if below_or_low:
        s += 6
    if dist_to_trigger_atr <= 0.5:
        s += 6
    if dist_to_trigger_atr <= 0.25:
        s += 3
    return float(min(135.0, s))


def entry_confirmed(bars: pd.DataFrame, trigger: float, vwap: float,
                    rvol_val: float, rvol_min: float) -> bool:
    """1-minute confirmation: the last COMPLETED bar must CLOSE below the
    trigger while below VWAP with acceptable relative volume - a failed
    bounce, not a one-tick spike."""
    if len(bars) < 2:
        return False
    last = bars.iloc[-2]  # iloc[-1] is the still-forming bar
    return (float(last["Close"]) < trigger
            and float(last["Close"]) < vwap
            and rvol_val >= rvol_min)


def remaining_reward_risk(px: float, stop: float, target: float) -> float:
    """R:R still on the table from the current price."""
    risk = stop - px
    if risk <= 0:
        return -np.inf
    return (px - target) / risk
