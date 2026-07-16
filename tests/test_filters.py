import pandas as pd

from mr_short.filters import select_candidates

CFG = {
    "filters": {
        "min_score": 50,
        "allowed_setups": ["DEAD-CAT BOUNCE", "BLOWOFF EXTENSION"],
        "fno_only": True,
        "max_adx": 35,
        "exclude_ban_list": False,  # keep tests offline
    },
    "risk": {"min_reward_risk": 2.0, "max_stop_distance_pct": 3.5,
             "max_new_trades_per_day": 3},
}


def row(sym, **over):
    base = {"symbol": sym, "score": 60, "setup": "DEAD-CAT BOUNCE", "fno": "Y",
            "adx": 20.0, "reward_risk": 3.0, "risk_pct": 2.0}
    base.update(over)
    return base


def test_each_gate_rejects_with_reason():
    scan = pd.DataFrame([
        row("GOOD"),
        row("LOWSCORE", score=40),
        row("WEAKCTX", setup="OVERBOUGHT (weak ctx)"),
        row("NOFNO", fno=""),
        row("HOTADX", adx=40),
        row("BADRR", reward_risk=1.5),
        row("WIDESTOP", risk_pct=5.0),
    ])
    selected, rejections = select_candidates(scan, CFG)
    assert list(selected["symbol"]) == ["GOOD"]
    reasons = dict(rejections)
    assert "score" in reasons["LOWSCORE"]
    assert "setup" in reasons["WEAKCTX"]
    assert "F&O" in reasons["NOFNO"]
    assert "ADX" in reasons["HOTADX"]
    assert "reward:risk" in reasons["BADRR"]
    assert "stop" in reasons["WIDESTOP"]


def test_no_truncation_before_sizing():
    # more passing candidates than the daily cap: ALL must come back,
    # sorted by score - the cap is applied after sizing
    scan = pd.DataFrame([row(f"S{i}", score=50 + i) for i in range(6)])
    selected, _ = select_candidates(scan, CFG)
    assert len(selected) == 6
    assert list(selected["score"]) == sorted(selected["score"], reverse=True)
