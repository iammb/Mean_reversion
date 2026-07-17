"""Live/intraday market data with a fallback chain ("by hook or by crook"):

  1. 1-minute bars from Yahoo Finance (delayed a minute or two for NSE -
     fine for confirmation logic; not for HFT)
  2. degrade to 5-minute bars if 1m comes back empty
  3. per-symbol soft-fail: a symbol with no data is reported, not fatal

A Kite Connect quote/historical provider can be slotted in here later for
true realtime - the interface is just get_intraday() / get_index().
"""

import datetime as dt

import pandas as pd

from ..utils import get_logger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9 fallback, not expected
    ZoneInfo = None

log = get_logger("live.data")

IST = ZoneInfo("Asia/Kolkata") if ZoneInfo else None
NIFTY = "^NSEI"


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST) if IST else dt.datetime.now()


def market_minutes_elapsed(ts: dt.datetime = None) -> float:
    """Minutes since 09:15 IST open, clamped to [0, 375]."""
    ts = ts or now_ist()
    open_t = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    return max(0.0, min(375.0, (ts - open_t).total_seconds() / 60.0))


def get_intraday(symbols, interval: str = "1m") -> dict:
    """{symbol: today's intraday OHLCV DataFrame}. Falls back 1m -> 5m."""
    import yfinance as yf

    tickers = [s + ".NS" for s in symbols]
    for iv in ([interval, "5m"] if interval == "1m" else [interval]):
        try:
            raw = yf.download(tickers, period="1d", interval=iv,
                              group_by="ticker", auto_adjust=False,
                              threads=True, progress=False)
        except Exception as e:
            log.warning(f"intraday download failed at {iv}: {e}")
            continue
        if raw is None or raw.empty:
            continue
        frames = {}
        if not isinstance(raw.columns, pd.MultiIndex):
            df = raw.dropna(how="all")
            if len(df):
                frames[symbols[0]] = df
        else:
            for s, t in zip(symbols, tickers):
                try:
                    df = raw[t].dropna(how="all")
                except KeyError:
                    continue
                if len(df):
                    frames[s] = df
        if frames:
            if iv != interval:
                log.warning(f"1m bars unavailable - degraded to {iv}")
            return frames
    log.warning("no intraday data from any interval")
    return {}


def get_index_change_pct() -> float:
    """Nifty 50 % change vs previous close (for relative strength)."""
    import yfinance as yf

    try:
        h = yf.download(NIFTY, period="2d", interval="1d",
                        auto_adjust=False, progress=False)
        closes = h["Close"]
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]
        prev = float(closes.iloc[-2]) if len(closes) >= 2 else float(closes.iloc[-1])
        intr = yf.download(NIFTY, period="1d", interval="5m",
                           auto_adjust=False, progress=False)
        last_ser = intr["Close"] if not intr.empty else closes
        if isinstance(last_ser, pd.DataFrame):
            last_ser = last_ser.iloc[:, 0]
        last = float(last_ser.dropna().iloc[-1])
        return 100.0 * (last - prev) / prev
    except Exception as e:
        log.warning(f"index change unavailable ({e}) - assuming 0")
        return 0.0
