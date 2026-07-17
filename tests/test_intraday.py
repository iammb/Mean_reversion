import numpy as np
import pandas as pd

from mr_short.live.intraday import (classify_gap, entry_confirmed,
                                    expected_volume_fraction, live_score,
                                    opening_range, remaining_reward_risk,
                                    rvol, session_vwap)


def bars(closes, vols=None, start="2026-07-17 09:15", freq="1min"):
    n = len(closes)
    vols = vols or [1000] * n
    idx = pd.date_range(start, periods=n, freq=freq)
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 0.5, "Low": c - 0.5,
                         "Close": c, "Volume": vols}, index=idx)


def test_vwap_weights_by_volume():
    b = bars([100, 200], vols=[1, 3])
    # TPs are 100 and 200; heavy volume on 200 pulls VWAP above midpoint
    assert session_vwap(b) > 150


def test_volume_fraction_monotonic():
    xs = [expected_volume_fraction(m) for m in (0, 15, 60, 195, 375)]
    assert xs == sorted(xs)
    assert xs[0] == 0.0 and xs[-1] == 1.0


def test_rvol_above_one_when_heavy():
    b = bars([100] * 10, vols=[50_000] * 10)
    # 500k traded in ~10 min vs 1M avg daily -> way above the ~10% expected
    assert rvol(b, avg_daily_volume=1_000_000, minutes=10) > 1.0


def test_opening_range():
    b = bars(list(range(100, 130)))  # 30 one-minute bars
    hi, lo = opening_range(b, minutes=15)
    assert lo == 100 - 0.5
    assert hi == 114 + 0.5  # bars 0..14 only


def test_classify_gap():
    assert classify_gap(103.0, 100.0, 98.0) == "UP_HARD"     # +3% gap up
    assert classify_gap(97.0, 100.0, 98.0) == "THROUGH"      # opened below trigger
    assert classify_gap(100.5, 100.0, 98.0) == "NORMAL"


def test_live_score_rewards_weakness():
    strong = live_score(50, below_vwap=False, rvol_val=0.5, rel_strength=1.0,
                        below_or_low=False, dist_to_trigger_atr=2.0)
    weak = live_score(50, below_vwap=True, rvol_val=1.6, rel_strength=-1.5,
                      below_or_low=True, dist_to_trigger_atr=0.2)
    assert weak > strong
    assert weak <= 135


def test_entry_needs_completed_bar_close_below_trigger():
    # last COMPLETED bar (iloc[-2]) closes at 97.9 < trigger 98 -> confirmed
    b = bars([100, 99, 97.9, 98.2])
    assert entry_confirmed(b, trigger=98.0, vwap=99.5, rvol_val=1.0, rvol_min=0.8)
    # completed bar above the trigger -> no entry even if the forming bar dips
    b2 = bars([100, 99, 98.6, 97.0])
    assert not entry_confirmed(b2, trigger=98.0, vwap=99.5, rvol_val=1.0, rvol_min=0.8)
    # below trigger but above VWAP -> bounce not failing -> no entry
    assert not entry_confirmed(b, trigger=98.0, vwap=97.0, rvol_val=1.0, rvol_min=0.8)
    # thin volume -> no entry
    assert not entry_confirmed(b, trigger=98.0, vwap=99.5, rvol_val=0.4, rvol_min=0.8)


def test_remaining_reward_risk():
    assert remaining_reward_risk(100, 105, 90) == 2.0
    assert remaining_reward_risk(106, 105, 90) == -np.inf  # past the stop
