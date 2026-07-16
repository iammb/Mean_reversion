"""Universe data: Nifty 500 constituents, F&O lot sizes, ban list, price history."""

import datetime as dt
import os
import time

import pandas as pd
import requests

from .utils import DATA_DIR, get_logger

log = get_logger("universe")

NIFTY500_CSV = os.path.join(DATA_DIR, "nifty500_constituents.csv")
FO_LOTS_CSV = os.path.join(DATA_DIR, "fo_mktlots.csv")
PRICE_CACHE = os.path.join(DATA_DIR, "price_cache.pkl")

BAN_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

LOOKBACK_DAYS = 400
MIN_BARS = 220


def load_universe() -> pd.DataFrame:
    df = pd.read_csv(NIFTY500_CSV)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"Company Name": "name", "Industry": "industry", "Symbol": "symbol"})
    df["symbol"] = df["symbol"].str.strip()
    return df[["symbol", "name", "industry"]]


def load_fno_lots() -> dict:
    """{symbol: current-month lot size} for stock F&O underlyings."""
    lots = {}
    if not os.path.exists(FO_LOTS_CSV):
        return lots
    with open(FO_LOTS_CSV) as f:
        for line in f.readlines()[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[1] and parts[1] not in INDEX_UNDERLYINGS:
                try:
                    lots[parts[1].upper()] = int(parts[2])
                except ValueError:
                    continue
    return lots


def fetch_ban_list() -> set:
    """Symbols in the NSE F&O security ban list. Soft-fails to empty set."""
    try:
        r = requests.get(BAN_LIST_URL, headers=UA, timeout=15)
        r.raise_for_status()
        banned = set()
        for line in r.text.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip():
                banned.add(parts[1].strip().upper())
        return banned
    except Exception as e:
        log.warning(f"could not fetch F&O ban list ({e}) - proceeding without it")
        return set()


def download_prices(symbols, use_cache: bool = False) -> dict:
    """{symbol: OHLCV DataFrame} via Yahoo Finance, cached to disk."""
    if use_cache and os.path.exists(PRICE_CACHE):
        age_h = (time.time() - os.path.getmtime(PRICE_CACHE)) / 3600.0
        if age_h < 20:
            log.info(f"using cached prices ({age_h:.1f}h old)")
            return pd.read_pickle(PRICE_CACHE)

    import yfinance as yf

    tickers = [s + ".NS" for s in symbols]
    start = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    frames = {}
    chunk = 50
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        log.info(f"downloading {i + 1}-{i + len(batch)} of {len(tickers)}")
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
    log.info(f"got usable history for {len(frames)} symbols")
    pd.to_pickle(frames, PRICE_CACHE)
    return frames
