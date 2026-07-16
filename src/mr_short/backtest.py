"""Historical backtest of the "Fade the Exhausted Bounce" short strategy.

Replays the exact scanner gates, trade-plan filters, entry/exit mechanics and
portfolio limits over daily history:

  signal day t   - stock passes every scanner gate + trade-plan filter at the
                   close (same code-path thresholds as scanner.py/filters.py)
  entry day t+1  - SL SELL: trigger 0.1% below day-t low, limit 0.25% lower.
                   Fills at the trigger on an intraday cross, at the open on a
                   gap below trigger (no fill if the gap blows through the
                   limit), else the order expires (entry_valid_sessions = 1).
  exits          - stop  : swing-high(t) + 0.25*ATR(t), gap-aware (fills at
                           open when the open gaps through it)
                   target: 20-EMA(t), gap-aware
                   both touched same day -> STOP assumed first (conservative)
                   time stop: exit at the open 7 sessions after entry
  costs          - `cost_bps` per side on notional (slippage + fees)

Known simplifications (disclosed with results):
  - TODAY'S Nifty 500 constituents and F&O list applied to the past
    (survivorship bias inflates results somewhat)
  - equity prices proxy the futures legs (no basis/rollover)
  - no historical F&O ban list, no earnings-date avoidance
  - portfolio sizing uses fractional quantities (no lot rounding)
"""

import datetime as dt
import os
import time

import numpy as np
import pandas as pd

from .universe import MIN_BARS
from .utils import DATA_DIR, get_logger

log = get_logger("backtest")

BT_CACHE = os.path.join(DATA_DIR, "price_cache_backtest.pkl")

COST_BPS_PER_SIDE = 5.0        # 0.05% of notional per side: fees + slippage
ENTRY_BUFFER = 0.001           # trigger = day_low * (1 - 0.1%)
ENTRY_LIMIT_SLIP = 0.0025      # limit = trigger * (1 - 0.25%)
TIME_STOP_SESSIONS = 7


# --------------------------------------------------------------------------
def download_history(symbols, years: int = 6, use_cache: bool = True) -> dict:
    """Long history for backtesting, cached separately from the scanner's."""
    if use_cache and os.path.exists(BT_CACHE):
        age_h = (time.time() - os.path.getmtime(BT_CACHE)) / 3600.0
        if age_h < 24 * 7:
            log.info(f"using cached backtest prices ({age_h:.1f}h old)")
            return pd.read_pickle(BT_CACHE)

    import yfinance as yf

    tickers = [s + ".NS" for s in symbols]
    start = (dt.date.today() - dt.timedelta(days=int(365.25 * years))).isoformat()
    frames = {}
    chunk = 50
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        log.info(f"downloading {i + 1}-{i + len(batch)} of {len(tickers)} ({years}y)")
        raw = yf.download(batch, start=start, group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
        if raw is None or raw.empty:
            continue
        if not isinstance(raw.columns, pd.MultiIndex):
            df = raw.dropna(how="all")
            if len(df) >= MIN_BARS:
                frames[batch[0].replace(".NS", "")] = df
            continue
        for t in batch:
            try:
                df = raw[t].dropna(how="all")
            except KeyError:
                continue
            if len(df) >= MIN_BARS:
                frames[t.replace(".NS", "")] = df
        time.sleep(0.5)
    log.info(f"got history for {len(frames)} symbols")
    pd.to_pickle(frames, BT_CACHE)
    return frames


# --------------------------------------------------------------------------
def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day features + signal flag, mirroring scanner.evaluate() and the
    trade-plan filters exactly, vectorised over the whole history."""
    from .indicators import adx, atr, rsi

    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    ema20 = close.ewm(span=20, adjust=False).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()

    rsi2 = rsi(close, 2)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    adx14, plus_di, minus_di = adx(high, low, close, 14)

    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    pct_b = (close - lower) / (upper - lower)
    zscore = (close - sma20) / std20
    stretch = (close - ema20) / atr14
    ret5 = close / close.shift(5) - 1.0
    up = close.diff() > 0
    consec = up.groupby((~up).cumsum()).cumsum().astype(float)
    vol_avg20 = vol.rolling(20).mean()
    vol_climax = up & (vol >= 2.0 * vol_avg20)
    turnover_cr = (close * vol).rolling(20).mean() / 1e7

    below200 = close < sma200
    falling200 = sma200 < sma200.shift(21)
    strong_up = (~below200) & (~falling200) & (close > sma50)
    hot_trend = (adx14 >= 35) & (plus_di > minus_di)
    high52 = close.rolling(252, min_periods=200).max()
    near_hi = close >= 0.98 * high52

    score = (
        np.where(rsi2 >= 90, 12, 0) + np.where(rsi2 >= 97, 6, 0)
        + np.where(rsi14 >= 65, 8, 0) + np.where(rsi14 >= 72, 4, 0)
        + np.where(pct_b >= 1.0, 10, 0)
        + np.where(zscore >= 2.0, 10, 0) + np.where(zscore >= 2.5, 5, 0)
        + np.where(stretch >= 2.0, 8, 0) + np.where(stretch >= 3.0, 4, 0)
        + np.where(consec >= 3, 6, 0) + np.where(consec >= 5, 3, 0)
        + np.where(ret5 >= 0.08, 6, 0) + np.where(ret5 >= 0.15, 4, 0)
        + np.where(vol_climax, 6, 0)
        + np.where(below200, 10, 0) + np.where(below200 & falling200, 5, 0)
        - np.where(hot_trend, 12, 0) - np.where(strong_up, 8, 0)
        - np.where(near_hi, 4, 0)
    )
    score = np.clip(score, 0, 100)

    secondary = ((pct_b >= 1.0).astype(int) + (zscore >= 2.0).astype(int)
                 + (rsi14 >= 65).astype(int) + (stretch >= 2.0).astype(int))
    gate = (rsi2 >= 85) & (secondary >= 2) & (close >= 50) & (turnover_cr >= 10)

    setup = np.select(
        [below200, (zscore >= 2.25) | ((pct_b >= 1.0) & (rsi2 >= 95))],
        ["DEAD-CAT BOUNCE", "BLOWOFF EXTENSION"], default="WEAK",
    )

    # trade-plan geometry, from signal-day values
    swing_high = high.rolling(5).max()
    stop = np.maximum(swing_high, close) + 0.25 * atr14
    target = ema20
    trigger = low * (1 - ENTRY_BUFFER)
    psr = stop - trigger
    rr = (trigger - target) / psr

    signal = (
        gate & (score >= 50) & (setup != "WEAK") & (adx14 <= 35)
        & (psr > 0) & (rr >= 2.0) & (psr / trigger <= 0.035)
    )

    return pd.DataFrame({
        "signal": signal.fillna(False), "score": score, "setup": setup,
        "trigger": trigger, "limit": trigger * (1 - ENTRY_LIMIT_SLIP),
        "stop": stop, "target": target, "rr": rr,
    }, index=df.index)


# --------------------------------------------------------------------------
def simulate_symbol(sym: str, df: pd.DataFrame, sig: pd.DataFrame,
                    start: dt.date) -> list:
    """Walk one symbol's signals chronologically; one trade at a time."""
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    dates = df.index.date
    n = len(df)
    trades = []
    busy_until = -1  # bar index until which the symbol is occupied

    sig_idx = np.flatnonzero(sig["signal"].to_numpy())
    for t in sig_idx:
        if t <= busy_until or t + 1 >= n or dates[t] < start:
            continue
        trigger, limit = sig["trigger"].iloc[t], sig["limit"].iloc[t]
        stop, target = sig["stop"].iloc[t], sig["target"].iloc[t]

        # ---- entry on day t+1 ----
        e = t + 1
        if o[e] <= trigger:
            if o[e] < limit:        # gapped through the limit - no fill
                busy_until = e
                continue
            entry_px = o[e]
        elif l[e] <= trigger:
            entry_px = trigger
        else:                       # bounce never failed - order expires
            busy_until = e
            continue

        # ---- exits ----
        exit_px = exit_reason = None
        exit_i = None
        for i in range(e, min(e + TIME_STOP_SESSIONS + 1, n)):
            if i - e >= TIME_STOP_SESSIONS:            # 7th session: close out
                exit_px, exit_reason, exit_i = o[i], "TIME_STOP", i
                break
            hi = h[i] if i > e else max(h[i], entry_px)  # entry-day range
            if i > e and o[i] >= stop:                 # gap through stop
                exit_px, exit_reason, exit_i = o[i], "STOP", i
                break
            if hi >= stop:                             # stop first: conservative
                exit_px, exit_reason, exit_i = stop, "STOP", i
                break
            if i > e and o[i] <= target:               # gap through target
                exit_px, exit_reason, exit_i = o[i], "TARGET", i
                break
            if l[i] <= target:
                exit_px, exit_reason, exit_i = target, "TARGET", i
                break
        if exit_px is None:                            # ran off the data edge
            exit_px, exit_reason, exit_i = c[n - 1], "OPEN_EOD", n - 1

        cost = COST_BPS_PER_SIDE / 10_000.0
        sell_eff = entry_px * (1 - cost)
        buy_eff = exit_px * (1 + cost)
        ret_pct = 100.0 * (sell_eff - buy_eff) / entry_px
        r_mult = (sell_eff - buy_eff) / (stop - entry_px)

        trades.append({
            "symbol": sym, "signal_date": dates[t], "entry_date": dates[e],
            "exit_date": dates[exit_i], "setup": sig["setup"].iloc[t],
            "score": float(sig["score"].iloc[t]),
            "entry": round(float(entry_px), 2), "stop": round(float(stop), 2),
            "target": round(float(target), 2), "exit": round(float(exit_px), 2),
            "exit_reason": exit_reason, "days_held": int(exit_i - e),
            "ret_pct": round(float(ret_pct), 3), "r_multiple": round(float(r_mult), 3),
        })
        busy_until = exit_i
    return trades


# --------------------------------------------------------------------------
def portfolio_sim(trades: pd.DataFrame, capital: float = 5_000_000,
                  risk_pct: float = 0.75, max_open: int = 5,
                  max_per_day: int = 3, max_trade_expo: float = 0.25,
                  max_total_expo: float = 0.60):
    """Chronological portfolio replay honouring the live risk limits.
    Fractional sizing (no lot rounding). Returns (taken_df, equity_curve)."""
    trades = trades.sort_values(["entry_date", "score"],
                                ascending=[True, False]).reset_index(drop=True)
    equity = capital
    open_pos = []          # (exit_date, notional)
    taken_rows = []
    curve = []
    for _, tr in trades.iterrows():
        d = tr["entry_date"]
        open_pos = [p for p in open_pos if p[0] > d]
        today_n = sum(1 for r in taken_rows if r["entry_date"] == d)
        if len(open_pos) >= max_open or today_n >= max_per_day:
            continue
        risk_amt = equity * risk_pct / 100.0
        per_share = tr["stop"] - tr["entry"]
        if per_share <= 0:
            continue
        qty = risk_amt / per_share
        qty = min(qty, max_trade_expo * equity / tr["entry"])
        open_notional = sum(p[1] for p in open_pos)
        room = max_total_expo * equity - open_notional
        qty = min(qty, max(0.0, room) / tr["entry"])
        if qty <= 0:
            continue
        pnl = qty * tr["entry"] * tr["ret_pct"] / 100.0
        equity += pnl
        open_pos.append((tr["exit_date"], qty * tr["entry"]))
        row = tr.to_dict()
        row.update(qty=round(qty, 1), pnl=round(pnl, 0), equity=round(equity, 0))
        taken_rows.append(row)
        curve.append((tr["exit_date"], equity))
    taken = pd.DataFrame(taken_rows)
    curve = pd.DataFrame(curve, columns=["date", "equity"]).groupby("date").last()
    return taken, curve


def summarize(trades: pd.DataFrame) -> dict:
    wins = trades[trades["ret_pct"] > 0]
    losses = trades[trades["ret_pct"] <= 0]
    gross_win = wins["ret_pct"].sum()
    gross_loss = -losses["ret_pct"].sum()
    return {
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if len(trades) else 0,
        "avg_win_pct": round(wins["ret_pct"].mean(), 2) if len(wins) else 0,
        "avg_loss_pct": round(losses["ret_pct"].mean(), 2) if len(losses) else 0,
        "expectancy_pct": round(trades["ret_pct"].mean(), 3) if len(trades) else 0,
        "avg_r": round(trades["r_multiple"].mean(), 3) if len(trades) else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else np.inf,
        "median_days_held": int(trades["days_held"].median()) if len(trades) else 0,
    }
