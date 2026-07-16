"""Instrument resolution: map an NSE symbol to the tradable instrument.

Equity (MIS_EQ): exchange NSE, tradingsymbol = the symbol itself.
Futures (NRML_FUT): exchange NFO, nearest-expiry stock future, resolved from
the live Kite instruments dump (never construct futures symbols by hand -
expiry conventions change; NSE moved F&O expiry to Tuesdays in 2025).
In dry-run (no Kite session) an indicative placeholder symbol is used.
"""

import datetime as dt
from dataclasses import dataclass

from ..utils import get_logger

log = get_logger("kite.instruments")

_nfo_cache = None


@dataclass
class Instrument:
    exchange: str        # NSE | NFO
    tradingsymbol: str
    lot_size: int        # 1 for equity
    expiry: str          # "" for equity
    indicative: bool     # True when resolved without a live session


def resolve(kite, symbol: str, product: str, lot_size_hint: int = 0) -> Instrument:
    if product == "MIS_EQ":
        return Instrument("NSE", symbol, 1, "", indicative=False)

    if product != "NRML_FUT":
        raise ValueError(f"unknown product {product}")

    if kite is None:
        # dry run: indicative current-month symbol; live mode resolves exactly
        tag = dt.date.today().strftime("%y%b").upper()
        return Instrument("NFO", f"{symbol}{tag}FUT", lot_size_hint or 1,
                          "(nearest)", indicative=True)

    global _nfo_cache
    if _nfo_cache is None:
        _nfo_cache = kite.instruments("NFO")
        log.info(f"loaded {len(_nfo_cache)} NFO instruments")

    today = dt.date.today()
    futs = [
        i for i in _nfo_cache
        if i["name"] == symbol and i["segment"] == "NFO-FUT" and i["expiry"] >= today
    ]
    if not futs:
        raise LookupError(f"no active future found for {symbol}")
    near = min(futs, key=lambda i: i["expiry"])
    if (near["expiry"] - today).days <= 2:
        # too close to expiry for a 7-session hold - roll to next month
        later = [i for i in futs if i["expiry"] > near["expiry"]]
        if later:
            near = min(later, key=lambda i: i["expiry"])
    return Instrument("NFO", near["tradingsymbol"], int(near["lot_size"]),
                      near["expiry"].isoformat(), indicative=False)
