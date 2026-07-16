from mr_short.risk import SizedTrade, apply_portfolio_limits, size_trade


def make_cfg(**over):
    cfg = {
        "capital": 5_000_000,
        "risk": {
            "risk_per_trade_pct": 0.75, "lot_risk_tolerance": 1.5,
            "max_positions": 5, "max_new_trades_per_day": 3,
            "max_exposure_per_trade_pct": 25, "max_total_exposure_pct": 60,
            "max_daily_loss_pct": 2.0, "min_reward_risk": 2.0,
            "max_stop_distance_pct": 3.5,
        },
        "entry": {"mode": "breakdown", "breakdown_buffer_pct": 0.10,
                  "product": "NRML_FUT"},
    }
    cfg.update(over)
    return cfg


def row(**over):
    base = {"symbol": "TEST", "entry": 1005.0, "day_low": 1001.0,
            "stop": 1020.0, "target_20ema": 940.0, "lot_size": 250}
    base.update(over)
    return base


def test_futures_sizing_respects_budget_and_exposure():
    # trigger entry = 1001*(1-0.001) = 999.999; risk/share ~20; budget 37,500
    t, why = size_trade(row(), make_cfg())
    assert why is None
    assert t.lots >= 1
    assert t.risk_amount <= 37_500 * 1.01
    assert t.notional <= 5_000_000 * 0.25


def test_rr_checked_at_trigger_price_not_close():
    # target barely below trigger -> R:R at trigger collapses -> reject
    t, why = size_trade(row(target_20ema=995.0), make_cfg())
    assert t is None and "reward:risk" in why


def test_single_lot_tolerance():
    # stop 1045 -> ~45/share risk; one 1000-share lot risks ~45k vs 37.5k
    # budget: allowed because 45k <= 1.5 x 37.5k
    t, why = size_trade(row(stop=1045.0, target_20ema=850.0, lot_size=1000),
                        make_cfg())
    assert why is None and t.lots == 1
    assert t.risk_amount > 37_500


def test_oversized_lot_skipped():
    t, why = size_trade(row(stop=1045.0, target_20ema=850.0, lot_size=2000),
                        make_cfg())
    assert t is None and "1 lot risks" in why


def test_mis_share_sizing():
    cfg = make_cfg()
    cfg["entry"]["product"] = "MIS_EQ"
    t, why = size_trade(row(), cfg)
    assert why is None and t.lots == 0
    assert t.qty > 0
    assert t.notional <= 5_000_000 * 0.25


def _sized(sym, notional=500_000):
    return SizedTrade(symbol=sym, product="NRML_FUT", qty=100, lots=1,
                      lot_size=100, entry=1000, stop=1020, target=940,
                      risk_amount=2000, notional=notional, reward_risk=3.0)


def test_open_positions_consume_slots():
    cfg = make_cfg()
    kept, skipped = apply_portfolio_limits(
        [_sized("A"), _sized("B")], cfg, open_positions=4, open_notional=0)
    assert [t.symbol for t in kept] == ["A"]
    assert skipped and "limit reached" in skipped[0][1]


def test_daily_cap_beats_free_slots():
    cfg = make_cfg()
    kept, _ = apply_portfolio_limits([_sized(s) for s in "ABCDE"], cfg)
    assert len(kept) == 3  # max_new_trades_per_day


def test_total_exposure_cap():
    cfg = make_cfg()
    kept, skipped = apply_portfolio_limits(
        [_sized("A", notional=1_000_000)], cfg,
        open_positions=1, open_notional=2_800_000)  # cap is 3M
    assert not kept and "exposure" in skipped[0][1]
