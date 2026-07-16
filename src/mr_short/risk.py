"""Risk management: position sizing and portfolio-level guardrails.

Sizing rule: risk a fixed % of capital between entry and stop.
    qty = floor(capital * risk% / (stop - entry))
Futures are rounded DOWN to whole lots; a single lot is allowed to
exceed the risk budget by at most `lot_risk_tolerance`, otherwise skip.
Every trade also respects a per-trade and total notional exposure cap.
"""

import math
from dataclasses import dataclass

from .utils import get_logger, inr

log = get_logger("risk")


@dataclass
class SizedTrade:
    symbol: str
    product: str          # NRML_FUT | MIS_EQ
    qty: int              # shares (equity) or contracts qty (futures: lots*lot_size)
    lots: int             # 0 for equity
    lot_size: int
    entry: float
    stop: float
    target: float
    risk_amount: float    # Rs lost if stopped out
    notional: float       # exposure at entry
    reward_risk: float


def size_trade(row, cfg: dict) -> tuple:
    """Size one candidate. Returns (SizedTrade|None, skip_reason|None)."""
    r = cfg["risk"]
    capital = cfg["capital"]
    product = cfg["entry"]["product"]
    budget = capital * r["risk_per_trade_pct"] / 100.0
    stop, target = float(row["stop"]), float(row["target_20ema"])

    # size off the price we would actually fill at: the breakdown trigger,
    # not the scan-day close - the stop is further from there
    if cfg["entry"]["mode"] == "breakdown":
        entry = float(row["day_low"]) * (1 - cfg["entry"]["breakdown_buffer_pct"] / 100.0)
    else:
        entry = float(row["entry"])

    per_share_risk = stop - entry
    if per_share_risk <= 0:
        return None, "stop not above entry"
    rr_at_entry = (entry - target) / per_share_risk
    if rr_at_entry < r["min_reward_risk"]:
        return None, (f"reward:risk at breakdown entry only {rr_at_entry:.2f} "
                      f"(target too close to trigger)")

    max_notional = capital * r["max_exposure_per_trade_pct"] / 100.0

    if product == "NRML_FUT":
        lot_size = int(row.get("lot_size") or 0)
        if lot_size <= 0:
            return None, "no lot size (not in F&O)"
        lots = math.floor(budget / (per_share_risk * lot_size))
        if lots == 0:
            # allow a single lot if it doesn't blow the budget by too much
            one_lot_risk = per_share_risk * lot_size
            if one_lot_risk <= budget * r["lot_risk_tolerance"]:
                lots = 1
            else:
                return None, (f"1 lot risks {inr(one_lot_risk)} > "
                              f"{r['lot_risk_tolerance']}x budget {inr(budget)}")
        qty = lots * lot_size
        while lots > 0 and qty * entry > max_notional:
            lots -= 1
            qty = lots * lot_size
        if lots == 0:
            return None, f"1 lot notional exceeds {r['max_exposure_per_trade_pct']}% of capital"
    else:  # MIS_EQ - intraday cash short
        lot_size, lots = 0, 0
        qty = math.floor(budget / per_share_risk)
        qty = min(qty, math.floor(max_notional / entry))
        if qty <= 0:
            return None, "qty rounds to zero"

    risk_amount = qty * per_share_risk
    notional = qty * entry
    rr = (entry - target) / per_share_risk
    return SizedTrade(
        symbol=row["symbol"], product=product, qty=qty, lots=lots,
        lot_size=lot_size, entry=entry, stop=stop, target=target,
        risk_amount=round(risk_amount, 0), notional=round(notional, 0),
        reward_risk=round(rr, 2),
    ), None


def apply_portfolio_limits(sized: list, cfg: dict, open_positions: int = 0,
                           open_notional: float = 0.0) -> tuple:
    """Enforce max positions, daily trade cap and total exposure across the
    batch, on top of what is already open. Returns (kept, skipped)."""
    r = cfg["risk"]
    capital = cfg["capital"]
    max_total = capital * r["max_total_exposure_pct"] / 100.0
    slots = max(0, r["max_positions"] - open_positions)
    slots = min(slots, r["max_new_trades_per_day"])
    running = open_notional
    kept, skipped = [], []
    for t in sized:
        if len(kept) >= slots:
            skipped.append((t.symbol, f"position/daily-trade limit reached "
                                      f"({open_positions} already open)"))
        elif running + t.notional > max_total:
            skipped.append((t.symbol, f"total exposure would exceed "
                                      f"{r['max_total_exposure_pct']}% of capital"))
        else:
            kept.append(t)
            running += t.notional
    return kept, skipped


def daily_loss_breached(kite, cfg: dict) -> bool:
    """Kill switch: day P&L (realised + mark-to-market, as Kite reports it)
    worse than max_daily_loss_pct. Checked before every entry batch (live only)."""
    if kite is None:
        return False
    try:
        pos = kite.positions()["day"]
        realised = sum(p.get("pnl", 0.0) for p in pos)
        limit = -cfg["capital"] * cfg["risk"]["max_daily_loss_pct"] / 100.0
        if realised <= limit:
            log.error(f"KILL SWITCH: day P&L {inr(realised)} <= limit {inr(limit)} - no new entries")
            return True
    except Exception as e:
        log.warning(f"could not check day P&L ({e}) - allowing entries")
    return False
