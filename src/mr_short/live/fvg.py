"""Fair Value Gap (FVG) / Inverse FVG entry logic for intraday shorts.

Concepts (ICT / smart-money):
  bearish FVG - in a 3-candle down displacement, candle1.low > candle3.high
                leaves an unfilled zone [candle3.high .. candle1.low].
                Price tends to retrace INTO the zone and reject.
  bullish FVG - mirror image (candle1.high < candle3.low).
  IFVG        - a bullish FVG that price later CLOSES below is "inverted":
                the zone flips from support to resistance. The morning
                bounce of a dead-cat setup paints bullish FVGs on the way
                up; the breakdown closes below them, and the retest of the
                inverted zone is a high-quality short.

Entry styles, in priority order (first match wins):
  IFVG retest > bearish-FVG retrace > plain break-retest of the level.
All give a tight stop just above a structural zone instead of the wide
daily swing-high stop - that is where the improved R:R comes from.
"""

from dataclasses import dataclass

import pandas as pd


@dataclass
class Zone:
    kind: str      # "FVG" (bearish) | "IFVG" (inverted bullish) | "LEVEL"
    bottom: float  # underside - the edge price retests from below
    top: float     # stop goes just above this
    born: int      # bar index at which the zone became active (tradeable)


def _completed(bars: pd.DataFrame) -> pd.DataFrame:
    """Drop the still-forming last bar."""
    return bars.iloc[:-1] if len(bars) > 1 else bars.iloc[0:0]


def bearish_fvgs(bars: pd.DataFrame, min_gap_pct: float) -> list:
    """Zones from 3-candle down displacements."""
    lo, hi = bars["Low"].to_numpy(), bars["High"].to_numpy()
    out = []
    for i in range(2, len(bars)):
        gap = lo[i - 2] - hi[i]
        if gap > 0 and 100.0 * gap / hi[i] >= min_gap_pct:
            out.append(Zone("FVG", bottom=float(hi[i]), top=float(lo[i - 2]), born=i))
    return out


def inverted_bullish_fvgs(bars: pd.DataFrame, min_gap_pct: float) -> list:
    """Bullish FVGs that a later candle CLOSED below -> resistance zones."""
    lo, hi = bars["Low"].to_numpy(), bars["High"].to_numpy()
    close = bars["Close"].to_numpy()
    out = []
    for i in range(2, len(bars)):
        gap = lo[i] - hi[i - 2]
        if gap > 0 and 100.0 * gap / hi[i - 2] >= min_gap_pct:
            zone_bottom, zone_top = float(hi[i - 2]), float(lo[i])
            for j in range(i + 1, len(bars)):        # first close below = inversion
                if close[j] < zone_bottom:
                    out.append(Zone("IFVG", bottom=zone_bottom, top=zone_top, born=j))
                    break
    return out


def active_zones(zones: list, bars: pd.DataFrame) -> list:
    """A zone dies once any later completed bar CLOSES above its top."""
    close = bars["Close"].to_numpy()
    alive = []
    for z in zones:
        if not any(close[j] > z.top for j in range(z.born + 1, len(bars))):
            alive.append(z)
    return alive


def retest_signal(bars: pd.DataFrame, zones: list, vwap: float,
                  zone_buffer_pct: float):
    """Rejection on the last COMPLETED bar: wicked into a zone from below,
    closed back under its bottom, while under VWAP. Returns (entry, stop,
    kind) or None. Nearest zone above price wins."""
    done = _completed(bars)
    if done.empty or not zones:
        return None
    last = done.iloc[-1]
    px = float(last["Close"])
    candidates = [z for z in zones
                  if z.born < len(done) - 1
                  and float(last["High"]) >= z.bottom
                  and px < z.bottom
                  and px < vwap]
    if not candidates:
        return None
    z = min(candidates, key=lambda z: z.bottom)       # nearest structure
    stop = z.top * (1 + zone_buffer_pct / 100.0)
    return px, stop, z.kind


def find_short_entry(bars: pd.DataFrame, break_level: float, break_idx: int,
                     vwap: float, min_gap_pct: float, zone_buffer_pct: float):
    """Full priority chain after a confirmed breakdown at bar `break_idx`:
    IFVG retest, else bearish-FVG retrace, else plain break-retest of the
    broken level. Returns (entry, stop, style) or None."""
    done = _completed(bars)
    if len(done) <= break_idx + 1:
        return None

    zones = active_zones(inverted_bullish_fvgs(done, min_gap_pct), done)
    sig = retest_signal(bars, zones, vwap, zone_buffer_pct)
    if sig:
        return sig

    post = [z for z in bearish_fvgs(done, min_gap_pct) if z.born >= break_idx]
    sig = retest_signal(bars, active_zones(post, done), vwap, zone_buffer_pct)
    if sig:
        return sig

    # fallback: break-retest - wick back up to the broken level, close below
    last = done.iloc[-1]
    px = float(last["Close"])
    if (float(last["High"]) >= break_level and px < break_level and px < vwap):
        swing = float(done["High"].iloc[-3:].max())
        stop = swing * (1 + zone_buffer_pct / 100.0)
        return px, stop, "LEVEL"
    return None
