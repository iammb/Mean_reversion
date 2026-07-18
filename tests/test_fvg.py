import pandas as pd

from mr_short.live.fvg import (Zone, active_zones, bearish_fvgs,
                               find_short_entry, inverted_bullish_fvgs,
                               retest_signal)


def bars(rows, start="2026-07-20 09:15"):
    """rows: list of (open, high, low, close) tuples -> 1m OHLCV frame."""
    idx = pd.date_range(start, periods=len(rows), freq="1min")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000
    return df


def test_bearish_fvg_detected():
    # candle0 low=100; candle2 high=99 -> zone [99, 100]
    b = bars([(101, 102, 100, 100.5),
              (100, 100.5, 98.5, 98.6),
              (98.6, 99.0, 97.5, 97.6)])
    zones = bearish_fvgs(b, min_gap_pct=0.05)
    assert len(zones) == 1
    z = zones[0]
    assert z.bottom == 99.0 and z.top == 100.0 and z.kind == "FVG"


def test_tiny_gap_ignored():
    b = bars([(101, 102, 100, 100.5),
              (100, 100.5, 99.99, 100.0),
              (100, 99.995, 99.5, 99.6)])
    assert bearish_fvgs(b, min_gap_pct=0.05) == []


def test_bullish_fvg_inversion():
    # up-displacement leaves bullish gap [101, 102]; later close below 101
    # inverts it into resistance
    b = bars([(100, 101, 99.5, 100.8),      # c0 high=101
              (101, 102.5, 100.9, 102.4),
              (102.4, 103.5, 102.0, 103.0),  # c2 low=102 -> gap [101, 102]
              (103, 103.2, 100.8, 100.9)])   # closes below 101 -> inversion
    zones = inverted_bullish_fvgs(b, min_gap_pct=0.05)
    assert len(zones) == 1
    z = zones[0]
    assert z.kind == "IFVG" and z.bottom == 101.0 and z.top == 102.0


def test_zone_dies_on_close_above_top():
    b = bars([(101, 102, 100, 100.5),
              (100, 100.5, 98.5, 98.6),
              (98.6, 99.0, 97.5, 97.6),
              (97.6, 100.6, 97.5, 100.5)])   # closes above zone top 100
    zones = bearish_fvgs(b, min_gap_pct=0.05)
    assert active_zones(zones, b) == []


def test_retest_rejection_entry():
    zone = Zone("FVG", bottom=99.0, top=100.0, born=2)
    # completed bar wicks to 99.4 (into zone) and closes 98.7 (back below)
    b = bars([(98, 98.5, 97.5, 98.0),
              (98.0, 98.8, 97.8, 98.2),
              (98.2, 98.9, 98.0, 98.4),
              (98.4, 99.4, 98.2, 98.7),      # the rejection bar
              (98.7, 98.8, 98.5, 98.6)])     # still-forming bar
    sig = retest_signal(b, [zone], vwap=99.5, zone_buffer_pct=0.05)
    assert sig is not None
    entry, stop, kind = sig
    assert entry == 98.7 and kind == "FVG"
    assert stop > 100.0                       # above zone top + buffer

    # same shape but above VWAP -> no entry
    assert retest_signal(b, [zone], vwap=98.0, zone_buffer_pct=0.05) is None


def test_break_retest_fallback():
    # no FVGs anywhere; price broke 100, wicked back to 100.2, closed 99.5
    b = bars([(101, 101.5, 100.6, 100.8),
              (100.8, 101.0, 100.5, 100.7),
              (100.7, 100.8, 99.4, 99.5),    # breakdown bar (idx 2)
              (99.5, 100.2, 99.3, 99.5),     # retest + rejection
              (99.5, 99.6, 99.2, 99.3)])     # forming
    sig = find_short_entry(b, break_level=100.0, break_idx=2, vwap=100.5,
                           min_gap_pct=0.05, zone_buffer_pct=0.05)
    assert sig is not None
    entry, stop, style = sig
    assert style == "LEVEL" and entry == 99.5 and stop > 100.2


def test_no_entry_without_retrace():
    # price just keeps falling after the break - no chase, no entry
    b = bars([(101, 101.5, 100.6, 100.8),
              (100.8, 100.9, 99.4, 99.5),
              (99.5, 99.6, 98.8, 98.9),
              (98.9, 99.0, 98.2, 98.3),
              (98.3, 98.4, 98.0, 98.1)])
    sig = find_short_entry(b, break_level=100.0, break_idx=1, vwap=100.0,
                           min_gap_pct=0.05, zone_buffer_pct=0.05)
    assert sig is None
