import numpy as np
import pandas as pd

from mr_short.indicators import adx, atr, consecutive_up_days, rsi


def test_rsi_all_gains_is_100():
    s = pd.Series(np.arange(1.0, 61.0))
    assert rsi(s, 14).iloc[-1] == 100.0


def test_rsi_flat_series_is_neutral():
    s = pd.Series([100.0] * 60)
    assert rsi(s, 14).iloc[-1] == 50.0


def test_rsi_bounded_on_random_walk():
    rng = np.random.default_rng(42)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 500)))
    vals = rsi(s, 2).dropna()
    assert ((vals >= 0) & (vals <= 100)).all()


def test_rsi_warmup_is_nan():
    s = pd.Series(np.arange(1.0, 61.0))
    assert pd.isna(rsi(s, 14).iloc[0])


def test_atr_positive_and_tracks_range():
    n = 100
    close = pd.Series(np.full(n, 100.0))
    high = close + 2.0
    low = close - 2.0
    a = atr(high, low, close, 14)
    assert a.iloc[-1] > 0
    assert abs(a.iloc[-1] - 4.0) < 0.5  # constant 4-point true range


def test_adx_finite():
    rng = np.random.default_rng(7)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    high, low = close + 1, close - 1
    a, plus, minus = adx(high, low, close, 14)
    assert np.isfinite(a.iloc[-1])
    assert np.isfinite(plus.iloc[-1]) and np.isfinite(minus.iloc[-1])


def test_consecutive_up_days():
    s = pd.Series([10.0, 11, 12, 11, 12, 13, 14])
    assert consecutive_up_days(s) == 3
    assert consecutive_up_days(pd.Series([5.0, 4.0])) == 0
