#!/usr/bin/env python3
"""
Nifty 500 short-side mean reversion scanner.

Strategy: "Fade the Exhausted Bounce"
Scans all Nifty 500 stocks for short-term overbought extremes that are
statistically likely to revert back down toward their 20-day mean.

Two setup types:
  1. DEAD-CAT BOUNCE  - stock in a long-term downtrend (below 200-DMA) that has
     rallied hard into overbought. Shorting weakness on a bounce is the
     highest-probability short in Indian markets.
  2. BLOWOFF EXTENSION - stock stretched to a statistical extreme above its
     20-day mean (z-score / ATR stretch / upper Bollinger) with climactic
     volume, regardless of long-term trend. Stricter thresholds apply.

India-specific handling:
  - Liquidity gate (min avg daily turnover) to avoid smallcap circuit traps.
  - F&O flag: cash-market shorts must be squared off intraday in India;
    only F&O stocks can be shorted overnight (via futures / options).
  - Strong-uptrend penalty: Indian equities have persistent upward drift;
    fading high-ADX uptrends gets run over, so those are penalised.

Usage:
  python scanner.py                 # full scan, prints top candidates
  python scanner.py --top 25        # show more rows
  python scanner.py --min-turnover 25   # stricter liquidity (in Rs crore)
  python scanner.py --fno-only      # only F&O stocks (shortable overnight)
  python scanner.py --cache         # reuse today's downloaded prices
"""

import argparse
import datetime as dt
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
RESULTS_DIR = os.path.join(HERE, "scan_results")
PRICE_CACHE = os.path.join(DATA_DIR, "price_cache.pkl")

LOOKBACK_DAYS = 400          # calendar days of history to download
MIN_BARS = 220               # need enough bars for the 200-DMA

# ---- eligibility gates ----
MIN_PRICE = 50.0             # Rs, avoid penny / circuit-prone names
DEFAULT_MIN_TURNOVER_CR = 10.0   # Rs crore, 20-day average daily turnover

# ---- core signal thresholds ----
RSI2_GATE = 85.0             # minimum RSI(2) to even be considered
RSI2_SIGNAL = 90.0
RSI2_EXTREME = 97.0
RSI14_SIGNAL = 65.0
RSI14_EXTREME = 72.0
ZSCORE_SIGNAL = 2.0
ZSCORE_EXTREME = 2.5
ATR_STRETCH_SIGNAL = 2.0
ATR_STRETCH_EXTREME = 3.0
RET5_SIGNAL = 0.08           # +8% in 5 days
RET5_EXTREME = 0.15
VOL_CLIMAX_MULT = 2.0        # volume >= 2x 20-day average on an up day
ADX_HOT = 35.0               # don't fade freight trains


# --------------------------------------------------------------------------
# indicators (plain pandas/numpy, Wilder smoothing where conventional)
# --------------------------------------------------------------------------

def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_loss.notna(), np.nan)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    alpha = 1.0 / period
    atr_ = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean() / atr_
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean() / atr_
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    return adx_, plus_di, minus_di


def consecutive_up_days(close: pd.Series) -> int:
    diffs = close.diff().iloc[::-1]
    count = 0
    for d in diffs:
        if pd.isna(d):
            break
        if d > 0:
            count += 1
        else:
            break
    return count


# --------------------------------------------------------------------------
# universe + data
# --------------------------------------------------------------------------

def load_universe():
    path = os.path.join(DATA_DIR, "nifty500_constituents.csv")
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Company Name": "name", "Industry": "industry", "Symbol": "symbol"})
    df["symbol"] = df["symbol"].str.strip()
    return df[["symbol", "name", "industry"]]


def load_fno_symbols():
    path = os.path.join(DATA_DIR, "fo_mktlots.csv")
    syms = set()
    if not os.path.exists(path):
        return syms
    with open(path) as f:
        for line in f.readlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 2:
                s = parts[1].strip().upper()
                if s and s.isascii():
                    syms.add(s)
    # drop index underlyings
    return syms - {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def download_prices(symbols, use_cache=False):
    if use_cache and os.path.exists(PRICE_CACHE):
        age_h = (time.time() - os.path.getmtime(PRICE_CACHE)) / 3600.0
        if age_h < 20:
            print(f"Using cached prices ({age_h:.1f}h old): {PRICE_CACHE}")
            return pd.read_pickle(PRICE_CACHE)

    import yfinance as yf

    tickers = [s + ".NS" for s in symbols]
    start = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    frames = {}
    chunk = 50
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        print(f"  downloading {i + 1}-{i + len(batch)} of {len(tickers)} ...", flush=True)
        raw = yf.download(
            batch, start=start, group_by="ticker", auto_adjust=True,
            threads=True, progress=False,
        )
        for t in batch:
            try:
                df = raw[t].dropna(how="all")
            except KeyError:
                continue
            if len(df) >= MIN_BARS:
                frames[t.replace(".NS", "")] = df
        time.sleep(0.5)
    print(f"  got usable history for {len(frames)} symbols")
    pd.to_pickle(frames, PRICE_CACHE)
    return frames


# --------------------------------------------------------------------------
# per-stock evaluation
# --------------------------------------------------------------------------

def evaluate(symbol, df, min_turnover_cr):
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
    swing_high = high.iloc[-5:].max()
    entry = c                                   # or next-day break of today's low
    stop = max(swing_high, c) + 0.25 * atr14
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
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target_20ema": round(target, 2),
        "risk_pct": round(100 * risk / entry, 2),
        "reward_risk": round(rr, 2) if not pd.isna(rr) else np.nan,
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Nifty 500 short-side mean reversion scanner")
    ap.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    ap.add_argument("--min-turnover", type=float, default=DEFAULT_MIN_TURNOVER_CR,
                    help="min 20d avg daily turnover in Rs crore (default 10)")
    ap.add_argument("--fno-only", action="store_true",
                    help="only stocks in the F&O segment (shortable overnight)")
    ap.add_argument("--cache", action="store_true", help="reuse cached prices if <20h old")
    args = ap.parse_args()

    universe = load_universe()
    fno = load_fno_symbols()
    print(f"Universe: {len(universe)} Nifty 500 stocks | F&O segment: {len(fno)} stocks")

    prices = download_prices(universe["symbol"].tolist(), use_cache=args.cache)

    rows = []
    for sym, df in prices.items():
        try:
            r = evaluate(sym, df, args.min_turnover)
        except Exception as e:
            print(f"  ! {sym}: {e}", file=sys.stderr)
            continue
        if r:
            r["fno"] = "Y" if sym in fno else ""
            rows.append(r)

    if not rows:
        print("\nNo stocks pass the overbought gate today. "
              "Mean reversion shorts need stretched conditions - none exist right now.")
        return

    res = pd.DataFrame(rows)
    meta = load_universe().set_index("symbol")
    res["name"] = res["symbol"].map(meta["name"])
    res["industry"] = res["symbol"].map(meta["industry"])
    if args.fno_only:
        res = res[res["fno"] == "Y"]
    res = res.sort_values("score", ascending=False).reset_index(drop=True)

    stamp = dt.date.today().isoformat()
    out_path = os.path.join(RESULTS_DIR, f"short_candidates_{stamp}.csv")
    res.to_csv(out_path, index=False)

    show = ["symbol", "score", "setup", "close", "rsi2", "rsi14", "zscore",
            "atr_stretch", "ret5_pct", "consec_up", "adx", "below_200dma",
            "fno", "entry", "stop", "target_20ema", "risk_pct", "reward_risk"]
    pd.set_option("display.width", 250)
    print(f"\n{len(res)} candidates | saved -> {out_path}\n")
    print(res[show].head(args.top).to_string(index=True))
    print(
        "\nNotes:\n"
        "  - Cash-market shorts in India are INTRADAY ONLY; hold overnight via futures/"
        "options only if fno == Y (check the current F&O ban list before trading).\n"
        "  - Entry: at close, or safer - next day only below today's low (confirmation).\n"
        "  - Stop: above the 5-day swing high + 0.25 ATR. Target: the 20-EMA. "
        "Time stop: exit after 7 sessions regardless.\n"
        "  - Skip names with earnings/results in the next 2 sessions.\n"
    )


if __name__ == "__main__":
    main()
