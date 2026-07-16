"""
"Fade the Exhausted Bounce" - Nifty 500 short-side mean reversion scan.

See README.md for the full strategy specification. evaluate() scores one
stock; run_scan() sweeps the universe and writes a ranked CSV.
"""

import datetime as dt
import os

import numpy as np
import pandas as pd

from .indicators import adx, atr, consecutive_up_days, rsi
from .universe import MIN_BARS, download_prices, load_fno_lots, load_universe
from .utils import SCAN_DIR, get_logger

log = get_logger("scanner")

MIN_PRICE = 50.0
DEFAULT_MIN_TURNOVER_CR = 10.0

RSI2_GATE = 85.0
RSI2_SIGNAL, RSI2_EXTREME = 90.0, 97.0
RSI14_SIGNAL, RSI14_EXTREME = 65.0, 72.0
ZSCORE_SIGNAL, ZSCORE_EXTREME = 2.0, 2.5
ATR_STRETCH_SIGNAL, ATR_STRETCH_EXTREME = 2.0, 3.0
RET5_SIGNAL, RET5_EXTREME = 0.08, 0.15
VOL_CLIMAX_MULT = 2.0
ADX_HOT = 35.0
STOP_ATR_MULT = 1.0   # backtest-validated: 0.25 ATR stops died 63% of the time;
                      # 1.0 ATR lifted win rate from 40% to 51% on DCB trades


def evaluate(symbol: str, df: pd.DataFrame, min_turnover_cr: float):
    """Score one stock; returns a result dict or None if it fails the gates."""
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    c = close.iloc[-1]

    if c < MIN_PRICE or len(df) < MIN_BARS:
        return None

    turnover_cr = (close * vol).rolling(20).mean().iloc[-1] / 1e7
    if pd.isna(turnover_cr) or turnover_cr < min_turnover_cr:
        return None

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    ema20 = close.ewm(span=20, adjust=False).mean()
    sma200 = close.rolling(200).mean()

    rsi2 = rsi(close, 2).iloc[-1]
    rsi14 = rsi(close, 14).iloc[-1]
    atr14 = atr(high, low, close, 14).iloc[-1]
    adx14, plus_di, minus_di = adx(high, low, close, 14)

    upper_band = sma20 + 2.0 * std20
    lower_band = sma20 - 2.0 * std20
    band_width = (upper_band - lower_band).iloc[-1]
    pct_b = (c - lower_band.iloc[-1]) / band_width if band_width > 0 else np.nan

    zscore = (c - sma20.iloc[-1]) / std20.iloc[-1] if std20.iloc[-1] > 0 else np.nan
    atr_stretch = (c - ema20.iloc[-1]) / atr14 if atr14 > 0 else np.nan
    ret5 = c / close.iloc[-6] - 1.0
    consec_up = consecutive_up_days(close)
    vol_avg20 = vol.rolling(20).mean().iloc[-1]
    up_day = close.iloc[-1] > close.iloc[-2]
    vol_climax = up_day and vol_avg20 > 0 and vol.iloc[-1] >= VOL_CLIMAX_MULT * vol_avg20

    below_200 = c < sma200.iloc[-1]
    sma200_falling = sma200.iloc[-1] < sma200.iloc[-21]
    sma50 = close.rolling(50).mean().iloc[-1]
    strong_uptrend = (not below_200) and (not sma200_falling) and c > sma50
    hot_trend = adx14.iloc[-1] >= ADX_HOT and plus_di.iloc[-1] > minus_di.iloc[-1]
    high_52w = close.rolling(252, min_periods=200).max().iloc[-1]
    near_52w_high = c >= 0.98 * high_52w

    # ---- eligibility gate: must be genuinely short-term overbought ----
    if pd.isna(rsi2) or rsi2 < RSI2_GATE:
        return None
    secondary = sum([
        pct_b >= 1.0,
        zscore >= ZSCORE_SIGNAL,
        rsi14 >= RSI14_SIGNAL,
        atr_stretch >= ATR_STRETCH_SIGNAL,
    ])
    if secondary < 2:
        return None

    # ---- composite score ----
    score = 0.0
    if rsi2 >= RSI2_SIGNAL:
        score += 12
        if rsi2 >= RSI2_EXTREME:
            score += 6
    if rsi14 >= RSI14_SIGNAL:
        score += 8
        if rsi14 >= RSI14_EXTREME:
            score += 4
    if pct_b >= 1.0:
        score += 10
    if zscore >= ZSCORE_SIGNAL:
        score += 10
        if zscore >= ZSCORE_EXTREME:
            score += 5
    if atr_stretch >= ATR_STRETCH_SIGNAL:
        score += 8
        if atr_stretch >= ATR_STRETCH_EXTREME:
            score += 4
    if consec_up >= 3:
        score += 6
        if consec_up >= 5:
            score += 3
    if ret5 >= RET5_SIGNAL:
        score += 6
        if ret5 >= RET5_EXTREME:
            score += 4
    if vol_climax:
        score += 6

    # trend context: shorting weakness is safer in India
    if below_200:
        score += 10
        if sma200_falling:
            score += 5
    # penalties: don't fade strength
    if hot_trend:
        score -= 12
    if strong_uptrend:
        score -= 8
    if near_52w_high:
        score -= 4

    score = max(0.0, min(100.0, score))

    if below_200:
        setup = "DEAD-CAT BOUNCE"
    elif zscore >= 2.25 or (pct_b >= 1.0 and rsi2 >= 95):
        setup = "BLOWOFF EXTENSION"
    else:
        setup = "OVERBOUGHT (weak ctx)"

    # ---- trade plan ----
    day_low = low.iloc[-1]
    swing_high = high.iloc[-5:].max()
    entry = c                                   # or next-day break of scan-day low
    stop = max(swing_high, c) + STOP_ATR_MULT * atr14
    target = ema20.iloc[-1]                     # revert to the mean
    risk = stop - entry
    reward = entry - target
    rr = reward / risk if risk > 0 else np.nan

    return {
        "symbol": symbol,
        "close": round(c, 2),
        "score": round(score, 0),
        "setup": setup,
        "rsi2": round(rsi2, 1),
        "rsi14": round(rsi14, 1),
        "pct_b": round(pct_b, 2),
        "zscore": round(zscore, 2),
        "atr_stretch": round(atr_stretch, 2),
        "ret5_pct": round(100 * ret5, 1),
        "consec_up": consec_up,
        "vol_climax": "Y" if vol_climax else "",
        "adx": round(adx14.iloc[-1], 1),
        "below_200dma": "Y" if below_200 else "",
        "turnover_cr": round(turnover_cr, 1),
        "day_low": round(day_low, 2),
        "atr14": round(atr14, 2),
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target_20ema": round(target, 2),
        "risk_pct": round(100 * risk / entry, 2),
        "reward_risk": round(rr, 2) if not pd.isna(rr) else np.nan,
    }


def run_scan(min_turnover_cr: float = DEFAULT_MIN_TURNOVER_CR,
             use_cache: bool = False) -> pd.DataFrame:
    """Scan the whole universe, save the ranked CSV, return the DataFrame."""
    universe = load_universe()
    fno_lots = load_fno_lots()
    log.info(f"universe: {len(universe)} stocks | F&O segment: {len(fno_lots)}")

    prices = download_prices(universe["symbol"].tolist(), use_cache=use_cache)

    rows = []
    for sym, df in prices.items():
        try:
            r = evaluate(sym, df, min_turnover_cr)
        except Exception as e:
            log.warning(f"{sym}: {e}")
            continue
        if r:
            r["fno"] = "Y" if sym in fno_lots else ""
            r["lot_size"] = fno_lots.get(sym, 0)
            rows.append(r)

    if not rows:
        log.info("no stocks pass the overbought gate today")
        return pd.DataFrame()

    res = pd.DataFrame(rows)
    meta = universe.set_index("symbol")
    res["name"] = res["symbol"].map(meta["name"])
    res["industry"] = res["symbol"].map(meta["industry"])
    res = res.sort_values("score", ascending=False).reset_index(drop=True)

    os.makedirs(SCAN_DIR, exist_ok=True)
    out_path = os.path.join(SCAN_DIR, f"short_candidates_{dt.date.today().isoformat()}.csv")
    res.to_csv(out_path, index=False)
    log.info(f"{len(res)} candidates -> {out_path}")
    return res
