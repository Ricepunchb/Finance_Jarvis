from datetime import datetime

from core import pnl
from core.pnl import KST


def _ts(month, day, hour=10, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=KST).timestamp()


def _intent(iid, symbol, side, qty, price, ts, status="FILLED", market="domestic", reason=None):
    return {"intent_id": iid, "symbol": symbol, "market": market, "side": side, "qty": qty,
            "price": price, "status": status, "created_at": ts, "reason": reason}


def _fill(iid, qty, price):
    return {"intent_id": iid, "qty": qty, "price": price, "filled_at": 0.0}


def _trades(intents, fills=(), ctx=None, fx_points=()):
    fx = pnl.make_fx(list(fx_points))
    trades, unpriced = pnl.build_trades(intents, list(fills), ctx or {}, fx)
    return trades, unpriced, fx


def test_local_date_uses_new_york_session_date_for_overseas_orders():
    # KST 10-01 00:27은 미국 현지로는 09-30 11:27(장중) -> 같은 세션의 일봉 날짜(09-30)와 맞아야 한다
    ts = _ts(10, 1, 0, 27)
    assert pnl.local_date("overseas", ts) == "2026-09-30"
    assert pnl.local_date("domestic", ts) == "2026-10-01"


def test_make_fx_returns_nearest_recorded_rate_and_extract_points_reads_context():
    rows = [
        {"ts": 100.0, "context_json": '{"bass_exrt": 1300.0, "total_equity": 1000}'},
        {"ts": 200.0, "context_json": '{"bass_exrt": 1400.0}'},
        {"ts": 300.0, "context_json": "not json"},
    ]
    fx_points, equity_points = pnl.extract_points(rows)
    fx = pnl.make_fx(fx_points)
    assert fx(120.0) == 1300.0 and fx(180.0) == 1400.0 and fx(0.0) == 1300.0 and fx(999.0) == 1400.0
    assert equity_points == [(100.0, 1000.0)]
    assert pnl.make_fx([])(5.0) == 1.0


def test_build_trades_ignores_unfilled_and_estimates_filled_without_fills():
    intents = [
        _intent("a", "005930", "buy", 3, 272000, _ts(9, 29), status="REJECTED"),
        _intent("b", "005930", "buy", 3, 272000, _ts(9, 29), status="SUBMITTED"),
        _intent("c", "005930", "sell", 4, None, _ts(9, 28), reason="TRAILING_TAKE_PROFIT"),
        _intent("d", "000660", "sell", 1, None, _ts(9, 28)),  # 가격을 알 방법이 없다
    ]
    ctx = {"c": {"price": 272000.0, "exit_check": {"avg_price": 272812.5}}}
    trades, unpriced, _ = _trades(intents, ctx=ctx)
    assert [t["intent_id"] for t in trades] == ["c"]
    assert trades[0]["estimated"] and trades[0]["price"] == 272000.0 and trades[0]["basis_hint_krw"] == 272812.5
    assert unpriced == ["d"]


def test_estimated_trailing_sells_reproduce_engine_daily_loss_accumulation():
    # 실제 DB의 09-28 매도 2건: 엔진이 기록한 daily_loss_accum(-66250)과 같아야 한다
    intents = [_intent("s1", "000660", "sell", 1, None, _ts(9, 28, 9, 52)),
               _intent("s2", "005930", "sell", 4, None, _ts(9, 28, 11, 56))]
    ctx = {"s1": {"price": 1810000.0, "exit_check": {"avg_price": 1873000.0}},
           "s2": {"price": 272000.0, "exit_check": {"avg_price": 272812.5}}}
    trades, _, _ = _trades(intents, ctx=ctx)
    events, _ = pnl.realized_events(trades, {})
    assert sum(e["pnl_krw"] for e in events) == -66250.0
    assert all(e["estimated"] for e in events)


def test_realized_uses_moving_average_cost():
    intents = [_intent("1", "A", "buy", 10, 100, _ts(9, 1)), _intent("2", "A", "buy", 10, 120, _ts(9, 2)),
               _intent("3", "A", "sell", 5, 130, _ts(9, 3)), _intent("4", "A", "sell", 15, 100, _ts(9, 4))]
    fills = [_fill("1", 10, 100), _fill("2", 10, 120), _fill("3", 5, 130), _fill("4", 15, 100)]
    trades, _, _ = _trades(intents, fills)
    events, state = pnl.realized_events(trades, {})
    assert [round(e["pnl_krw"], 6) for e in events] == [100.0, -150.0]  # (130-110)*5, (100-110)*15
    assert not any(e["estimated"] for e in events)
    assert state["A"]["qty"] == 0


def test_partial_fills_of_one_order_are_separate_trades_at_order_date():
    intents = [_intent("1", "A", "buy", 3, 100, _ts(9, 1, 15, 20))]
    fills = [_fill("1", 1, 99), _fill("1", 2, 101)]
    trades, _, _ = _trades(intents, fills)
    assert [(t["qty"], t["price"], t["date"]) for t in trades] == [(1.0, 99.0, "2026-09-01"), (2.0, 101.0, "2026-09-01")]


def test_sell_beyond_known_holdings_is_flagged_estimated():
    intents = [_intent("1", "A", "sell", 5, 130, _ts(9, 3))]
    trades, _, _ = _trades(intents, [_fill("1", 5, 130)])
    events, _ = pnl.realized_events(trades, {})
    assert events[0]["estimated"] and events[0]["pnl_krw"] == 0.0  # 원가 불명 -> 손익 0 + 추정 표시


def test_opening_position_is_rewound_from_current_positions():
    # 017670 실제 사례: fills는 11주x6회=66주인데 보유는 71주 -> 조회 이전부터 5주 보유
    intents = [_intent(str(i), "017670", "buy", 11, 85000, _ts(9, 29, 9 + i)) for i in range(6)]
    fills = [_fill(str(i), 11, 85000) for i in range(6)]
    trades, _, fx = _trades(intents, fills)
    positions = {"017670": {"qty": 71.0, "avg_price": 85395.07, "entry_avg_price": 87000.0}}
    opening, mismatch = pnl.compute_opening(trades, positions, fx)
    assert opening["017670"]["qty"] == 5.0 and not mismatch
    implied = (85395.07 * 71 - 66 * 85000) / 5
    assert abs(opening["017670"]["avg_local"] - implied) < 1e-6


def test_positions_contradicting_fills_are_flagged_instead_of_negative_holdings():
    # 해외: 매수 체결은 31주인데 positions는 0주(잔고조회가 해외 보유를 못 읽는 경우)
    intents = [_intent("1", "NFLX", "buy", 31, 70.0, _ts(10, 1, 0, 27), market="overseas")]
    trades, _, fx = _trades(intents, [_fill("1", 31, 70.0)], fx_points=[(0.0, 1355.7)])
    opening, mismatch = pnl.compute_opening(trades, {"NFLX": {"qty": 0.0, "avg_price": 0.0, "market": "overseas"}}, fx)
    assert opening["NFLX"]["qty"] == 0.0 and mismatch == {"NFLX": 31.0}


def test_overseas_realized_pnl_converts_with_buy_and_sell_rates():
    intents = [_intent("1", "T", "buy", 2, 100.0, _ts(9, 1), market="overseas"),
               _intent("2", "T", "sell", 2, 110.0, _ts(9, 8), market="overseas")]
    fx_points = [(_ts(9, 1), 1300.0), (_ts(9, 8), 1400.0)]
    trades, _, _ = _trades(intents, [_fill("1", 2, 100.0), _fill("2", 2, 110.0)], fx_points=fx_points)
    events, _ = pnl.realized_events(trades, {})
    assert events[0]["pnl_local"] == 20.0
    assert events[0]["pnl_krw"] == 2 * (110.0 * 1400 - 100.0 * 1300)  # 환차익 포함


def _mtm_fixture():
    intents = [_intent("1", "A", "buy", 10, 100, _ts(9, 1)), _intent("2", "A", "sell", 4, 120, _ts(9, 3))]
    trades, _, fx = _trades(intents, [_fill("1", 10, 100), _fill("2", 4, 120)])
    closes = {"A": {"2026-08-31": 98.0, "2026-09-01": 105.0, "2026-09-02": 110.0, "2026-09-03": 115.0, "2026-09-04": 112.0}}
    axis = pnl.build_axis(list(closes["A"]) + [t["date"] for t in trades], "2026-09-01", "2026-09-04")
    return trades, closes, axis, fx


def test_daily_mtm_pnl_telescopes_to_final_value_plus_cash_flows():
    trades, closes, axis, fx = _mtm_fixture()
    mtm = pnl.daily_mtm(trades, {}, closes, {"A": "domestic"}, axis, fx)["A"]
    assert mtm["2026-09-01"]["pnl_krw"] == 10 * 105 - 10 * 100          # 매수 당일: 종가-매수가
    assert mtm["2026-09-02"]["pnl_krw"] == 10 * (110 - 105)
    assert mtm["2026-09-03"]["pnl_krw"] == 6 * 115 - 10 * 110 + 4 * 120  # 일부 매도
    final_value = 6 * 112
    cash = -10 * 100 + 4 * 120
    assert sum(r["pnl_krw"] for r in mtm.values()) == final_value + cash
    assert mtm["2026-09-02"]["ret"] == 50 / (10 * 105)


def test_portfolio_daily_equals_sum_of_symbol_rows_and_tracks_drawdown():
    trades, closes, axis, fx = _mtm_fixture()
    intents = [_intent("3", "B", "buy", 1, 1000, _ts(9, 1))]
    trades_b, _, _ = _trades(intents, [_fill("3", 1, 1000)])
    closes["B"] = {"2026-09-01": 1000.0, "2026-09-02": 900.0, "2026-09-03": 950.0, "2026-09-04": 1000.0}
    mtm = pnl.daily_mtm(trades + trades_b, {}, closes, {"A": "domestic", "B": "domestic"}, axis, fx)
    daily = pnl.portfolio_daily(mtm, axis)
    for row in daily:
        assert row["pnl_krw"] == sum(m[row["date"]]["pnl_krw"] for m in mtm.values())
    assert daily[-1]["cum_pnl_krw"] == sum(r["pnl_krw"] for m in mtm.values() for r in m.values())
    assert pnl.max_drawdown(daily) < 0  # 9/2에 B가 -10%


def test_mtm_with_opening_position_and_holiday_gap_forward_fills_close():
    # 9/2에 종가가 없는 날(휴장)은 직전 종가를 이월 -> 손익 0
    closes = {"A": {"2026-09-01": 100.0, "2026-09-03": 110.0}}
    axis = ["2026-09-01", "2026-09-02", "2026-09-03"]
    mtm = pnl.daily_mtm([], {"A": 5.0}, closes, {"A": "domestic"}, axis, pnl.make_fx([]))["A"]
    assert mtm["2026-09-01"]["pnl_krw"] == 0.0 and mtm["2026-09-02"]["pnl_krw"] == 0.0
    assert mtm["2026-09-03"]["pnl_krw"] == 50.0


def test_mtm_overseas_includes_fx_effect():
    intents = [_intent("1", "T", "buy", 1, 100.0, _ts(9, 1), market="overseas")]
    trades, _, fx = _trades(intents, [_fill("1", 1, 100.0)], fx_points=[(_ts(9, 1), 1000.0), (_ts(9, 2, 23), 1100.0)])
    trade_date = trades[0]["date"]
    closes = {"T": {trade_date: 100.0, "2026-09-02": 100.0}}
    axis = pnl.build_axis(list(closes["T"]), trade_date, "2026-09-02")
    mtm = pnl.daily_mtm(trades, {}, closes, {"T": "overseas"}, axis, fx)["T"]
    assert mtm["2026-09-02"]["pnl_krw"] == 100.0 * 1100 - 100.0 * 1000  # 주가 불변, 환율만 +100원


def test_summarize_symbols_splits_realized_unrealized_and_flags_estimates():
    intents = [_intent("1", "A", "buy", 10, 100, _ts(9, 1)), _intent("2", "A", "sell", 4, 120, _ts(9, 3))]
    trades, _, fx = _trades(intents, [_fill("1", 10, 100), _fill("2", 4, 120)])
    events, state = pnl.realized_events(trades, {})
    summary = pnl.summarize_symbols(events, trades, state, {}, {"A": 6.0}, {"A": 130.0}, {"A": "domestic"}, {}, {}, 1.0)
    row = summary[0]
    assert row["realized_krw"] == 80.0 and row["unrealized_krw"] == 180.0
    assert row["total_pnl_krw"] == 260.0 and row["invested_krw"] == 1000.0 and row["roi"] == 0.26
    assert row["win_rate"] == 1.0 and row["trade_count"] == 2 and not row["estimated"]


def test_equity_by_day_keeps_last_value_per_kst_day():
    points = [(_ts(9, 1, 9), 100.0), (_ts(9, 1, 15), 110.0), (_ts(9, 2, 9), 105.0)]
    assert pnl.equity_by_day(points) == [{"date": "2026-09-01", "equity_krw": 110.0},
                                         {"date": "2026-09-02", "equity_krw": 105.0}]


def test_equity_outliers_from_partial_account_reads_are_dropped():
    points = [(float(i), v) for i, v in enumerate([354e6, 10e6, 355e6, 354.5e6, 10e6, 356e6])]
    kept, dropped = pnl.filter_equity_outliers(points)
    assert dropped == 2 and all(v > 300e6 for _, v in kept)
    assert pnl.filter_equity_outliers(points[:2]) == (points[:2], 0)


def test_anchor_first_day_excludes_price_move_before_recording_started():
    closes = {"A": {"2026-08-31": 90.0, "2026-09-01": 100.0, "2026-09-02": 110.0}}
    axis = ["2026-09-01", "2026-09-02"]
    fx = pnl.make_fx([])
    plain = pnl.daily_mtm([], {"A": 5.0}, closes, {"A": "domestic"}, axis, fx)["A"]
    anchored = pnl.daily_mtm([], {"A": 5.0}, closes, {"A": "domestic"}, axis, fx, anchor_first_day=True)["A"]
    assert plain["2026-09-01"]["pnl_krw"] == 50.0      # 8/31 -> 9/1 변동 포함
    assert anchored["2026-09-01"]["pnl_krw"] == 0.0    # 기록 시작일엔 기준가=당일 종가
    assert anchored["2026-09-02"]["pnl_krw"] == 50.0
