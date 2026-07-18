"""Trade selection: turn a raw scan CSV into the shortlist worth trading.

Every rejection is recorded with a reason so you can audit why a name
was dropped instead of wondering where it went.
"""

import pandas as pd

from .universe import fetch_ban_list
from .utils import get_logger

log = get_logger("filters")


def select_candidates(scan: pd.DataFrame, cfg: dict):
    """Apply config filters. Returns (selected_df, rejections list of (symbol, reason))."""
    f = cfg["filters"]
    r = cfg["risk"]
    # in fvg_retrace mode the stop comes from the intraday zone, so the
    # EOD wide-stop geometry gates (reward:risk, stop distance) don't apply
    fvg_mode = cfg.get("entry", {}).get("mode") == "fvg_retrace"
    rejections = []
    banned = fetch_ban_list() if f.get("exclude_ban_list", True) else set()
    if banned:
        log.info(f"F&O ban list today: {sorted(banned)}")

    keep = []
    for _, row in scan.iterrows():
        sym = row["symbol"]
        if row["score"] < f["min_score"]:
            rejections.append((sym, f"score {row['score']:.0f} < {f['min_score']}"))
        elif row["setup"] not in f["allowed_setups"]:
            rejections.append((sym, f"setup '{row['setup']}' not allowed"))
        elif f.get("fno_only", True) and row.get("fno") != "Y":
            rejections.append((sym, "not in F&O (cash shorts are intraday-only)"))
        elif sym in banned:
            rejections.append((sym, "in F&O ban list"))
        elif row["adx"] > f["max_adx"]:
            rejections.append((sym, f"ADX {row['adx']:.0f} > {f['max_adx']} (trending)"))
        elif not fvg_mode and row["reward_risk"] < r["min_reward_risk"]:
            rejections.append((sym, f"reward:risk {row['reward_risk']:.2f} < {r['min_reward_risk']}"))
        elif not fvg_mode and row["risk_pct"] > r["max_stop_distance_pct"]:
            rejections.append((sym, f"stop {row['risk_pct']:.1f}% away > {r['max_stop_distance_pct']}%"))
        else:
            keep.append(row)

    # return ALL passing candidates, best first - sizing may reject some, so
    # the daily trade cap is applied after sizing (in apply_portfolio_limits),
    # letting lower-ranked names fill slots that sizing rejections free up
    selected = pd.DataFrame(keep)
    if not selected.empty:
        selected = selected.sort_values("score", ascending=False).reset_index(drop=True)
    return selected, rejections
