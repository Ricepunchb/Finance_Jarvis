from datetime import datetime

import pandas as pd
import pytest

from core import discovery_signals as ds
from core import master_files

NOW = 1_800_000_000.0
DAY = 86400


def test_news_acceleration_detects_spike():
    published = [NOW - 0.5 * DAY] * 6 + [NOW - 4 * DAY, NOW - 5 * DAY, NOW - 8 * DAY]
    result = ds.news_acceleration(published, NOW, truncated=False)
    assert result["news_3d"] == 6
    # 최근 3일 일평균 2건 / 이전 7일 일평균 3/7건
    assert result["news_accel"] == pytest.approx(2 / (3 / 7), rel=0.01)


def test_news_acceleration_floor_when_no_prior_news():
    result = ds.news_acceleration([NOW - DAY] * 3, NOW, truncated=False)
    assert result["news_accel"] == pytest.approx(1 / 0.3, rel=0.01)


def test_news_acceleration_skips_ratio_when_truncated_window_too_short():
    # 50건이 전부 최근 3일 안에 몰려 있으면 기준 구간을 관측하지 못한 것 - 가속도를 만들지 않는다
    published = [NOW - i * 3000 for i in range(50)]
    result = ds.news_acceleration(published, NOW, truncated=True)
    assert result == {"news_3d": 50}


def _df(volumes, closes):
    return pd.DataFrame({"volume": volumes, "close": closes})


def test_volume_accumulation_quiet_price_rising_volume():
    result = ds.volume_accumulation(_df([100] * 20 + [300] * 5, [1000] * 25))
    # 최근 20봉 = 100x15 + 300x5 -> 평균 150, 최근 5봉 평균 300
    assert result["vol_ratio_5_20"] == 2.0
    assert result["ret_5d_pct"] == 0


def test_volume_accumulation_needs_enough_bars():
    assert ds.volume_accumulation(_df([1] * 10, [1] * 10)) == {}


def test_theme_laggards_picks_largest_unmoved_peers():
    memberships = [{"theme_code": "240", "theme_name": "PCB", "symbol": s} for s in ["A", "B", "C", "D", "E"]]
    movers = {"A": 12.0, "B": 8.0}
    eligible = {
        s: {"name": s + "전자", "market_cap_eok": cap}
        for s, cap in {"A": 100, "B": 90, "C": 5000, "D": 3000, "E": 10, }.items()
    }
    result = ds.theme_laggards(memberships, movers, eligible, {s: e["name"] for s, e in eligible.items()})
    assert set(result) == {"C", "D", "E"}
    assert result["C"] == {"theme": "PCB", "movers": ["A전자", "B전자"]}


def test_theme_laggards_ignores_single_mover_and_huge_themes():
    small = [{"theme_code": "1", "theme_name": "t", "symbol": s} for s in ["A", "B"]]
    huge = [{"theme_code": "2", "theme_name": "big", "symbol": f"S{i}"} for i in range(ds.THEME_MAX_MEMBERS + 1)]
    eligible = {m["symbol"]: {"name": m["symbol"], "market_cap_eok": 1} for m in small + huge}
    movers = {"A": 10.0, "S0": 10.0, "S1": 10.0}
    assert ds.theme_laggards(small + huge, movers, eligible, {}) == {}


def test_theme_laggards_skips_listing_year_themes():
    memberships = [{"theme_code": "027", "theme_name": "2024 신규 상장주", "symbol": s} for s in ["A", "B", "C"]]
    eligible = {s: {"name": s, "market_cap_eok": 1} for s in "ABC"}
    assert ds.theme_laggards(memberships, {"A": 10.0, "B": 10.0}, eligible, {}) == {}


def test_early_signal_requires_price_not_yet_moved():
    quiet = {"news_accel": 3.0, "ret_5d_pct": 1.0}
    moved = {"news_accel": 3.0, "ret_5d_pct": 15.0}
    score, reasons = ds.early_signal(quiet)
    assert score > 0 and len(reasons) == 1
    assert ds.early_signal(moved) == (0.0, [])


def test_early_signal_combines_and_caps():
    entry = {
        "news_accel": 10.0, "vol_ratio_5_20": 5.0, "ret_5d_pct": 0.5,
        "theme_laggard": {"theme": "PCB", "movers": ["A", "B"]},
    }
    score, reasons = ds.early_signal(entry)
    assert score == 1.0
    assert len(reasons) == 3


def test_composite_score_angle_is_largest_weighted_component():
    weights = {"tech": 0.3, "value": 0.3, "early": 0.25, "broker": 0.15}
    score, angle = ds.composite_score({"tech": 0.2, "value": 0.9, "early": 0.1, "broker": 0}, weights)
    assert angle == "value"
    assert score == pytest.approx(0.06 + 0.27 + 0.025)
    assert ds.composite_score({}, weights) == (0, "momentum")


def test_select_with_quota_keeps_minority_angles():
    entries = [{"symbol": f"M{i}", "angle": "momentum", "score": 0.9 - i * 0.01} for i in range(10)]
    entries += [{"symbol": "V1", "angle": "value", "score": 0.2}, {"symbol": "Z", "angle": "early", "score": 0}]
    chosen = ds.select_with_quota(entries, top_n=5, quota={"value": 1, "momentum": 2}, exclude={"M0"})
    symbols = [e["symbol"] for e in chosen]
    assert "V1" in symbols and "M0" not in symbols and "Z" not in symbols
    assert len(chosen) == 5
    assert symbols == sorted(symbols, key=lambda s: -next(e["score"] for e in entries if e["symbol"] == s))


def test_group_research_reports_applies_lookback():
    now = datetime(2026, 9, 28)
    reports = [
        {"symbol": "000720", "broker": "유안타", "title": "원전", "date": "20260921", "read_count": 1},
        {"symbol": "000720", "broker": "미래에셋", "title": "수주", "date": "20260901", "read_count": 1},
    ]
    grouped = ds.group_research_reports(reports, 14, now)
    assert grouped["000720"]["report_count"] == 1
    assert ds.broker_score(grouped["000720"]["reports"]) == pytest.approx(1 / 3)


def _master_line(market: str, symbol: str, name: str, **fields: str) -> str:
    layout = master_files._LAYOUTS[market]
    tail = [" "] * layout["tail"]
    defaults = {"group": "ST", "etp": " ", "spac": "N", "suspended": "N", "liquidation": "N", "managed": "N",
                "warning": "00", "preferred": "0", "sector": "0018", "roe": "000005.19", "market_cap": "000134851"}
    if "caution" in layout:
        defaults["caution"] = "N"
    for key, value in {**defaults, **fields}.items():
        start, end = layout[key]
        assert len(value) == end - start
        tail[start:end] = value
    return f"{symbol:<9}{'KR7' + symbol + '000':<12}{name}" + "".join(tail) + "\n"


@pytest.mark.parametrize("market", ["KOSPI", "KOSDAQ"])
def test_parse_stock_master_line(market):
    row = master_files.parse_stock_master_line(_master_line(market, "000720", "현대건설"), market)
    assert row == {
        "symbol": "000720", "name": "현대건설", "market": market, "sector_code": "0018",
        "market_cap_eok": 134851.0, "roe": 5.19, "is_excluded": False,
    }


@pytest.mark.parametrize("fields", [
    {"preferred": "1"}, {"group": "EF"}, {"suspended": "Y"}, {"managed": "Y"}, {"warning": "01"}, {"spac": "Y"},
])
def test_parse_stock_master_line_exclusions(fields):
    row = master_files.parse_stock_master_line(_master_line("KOSPI", "005935", "삼성전자우", **fields), "KOSPI")
    assert row["is_excluded"] is True


def test_parse_theme_line():
    assert master_files.parse_theme_line("240PCB" + " " * 37 + "353200   \n") == {
        "theme_code": "240", "theme_name": "PCB", "symbol": "353200",
    }


def test_merge_sources_multi_source_first_then_round_robin():
    from core.discovery_sources import merge_sources
    eligible = {s: {} for s in ["B1", "B2", "B3", "B4", "M1", "M2", "X"]}
    by_source = {
        "broker": {"B1": {}, "B2": {}, "B3": {}, "B4": {}, "X": {}},
        "momentum": {"M1": {}, "X": {}, "M2": {}, "NOT_ELIGIBLE": {}},
    }
    merged = merge_sources(by_source, eligible, limit=4)
    assert list(merged) == ["X", "B1", "M1", "B2"]
    assert set(merged["X"]) == {"broker", "momentum"}


def test_finalize_scores_ranks_components_within_pool():
    weights = {"tech": 0.5, "value": 0.5, "early": 0.0, "broker": 0.0}
    entries = [
        {"symbol": "V1", "raw_components": {"tech": 0.0, "value": 0.90}},
        {"symbol": "V2", "raw_components": {"tech": 0.0, "value": 0.85}},
        {"symbol": "T1", "raw_components": {"tech": 0.30, "value": 0.80}},
        {"symbol": "N", "raw_components": {}},
    ]
    ds.finalize_scores(entries, weights)
    by = {e["symbol"]: e for e in entries}
    # 절대값은 value가 훨씬 크지만, 유일한 tech 보유 종목은 tech 백분위 1.0을 받는다
    assert by["T1"]["components"] == {"tech": 1.0, "value": pytest.approx(1 / 3, abs=0.001), "early": 0.0, "broker": 0.0}
    assert by["T1"]["angle"] == "momentum"
    assert by["V1"]["angle"] == "value" and by["V1"]["angle_label"] == "저평가"
    assert by["N"]["score"] == 0


def test_build_thesis_orders_evidence_and_caps_lines():
    entry = {
        "sources": {
            "broker": {"days": 14, "report_count": 2, "reports": [
                {"broker": "유안타증권", "title": "독보적인 원전 올 콜렉터!"}, {"broker": "미래에셋증권", "title": "수주"}]},
            "momentum": {"rise_pct": 5.2, "vol_increase_pct": 180.0},
            "news": {"mentions": 7},
        },
        "target_gap_pct": 0.35, "per": 12.3,
        "tech": {"direction": "BUY", "strength": 0.72},
    }
    lines = ds.build_thesis(entry, ["조기신호 근거"])
    assert lines[0].startswith("최근 14일 증권사 리포트 2건: 유안타증권 '독보적인 원전 올 콜렉터!'")
    assert lines[1] == "컨센서스 목표가 대비 상승여력 35%, PER 12.3"
    assert lines[2] == "조기신호 근거"
    assert lines[3] == "일봉 기술적 매수 시그널(RSI/MACD/볼린저) 강도 0.72"
    assert len(lines) == 4
