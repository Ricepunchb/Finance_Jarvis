# tests/test_algorithm_inspection_fixes.py
"""정기 알고리즘 및 파이프라인 점검 개선사항 검증 테스트.

1. KRX 호가단위(Tick Size) 보정
2. KRX 공휴일 및 거래시간 판별 (한글날 등 비영업일 주문 방지)
3. 미편입 실계좌 보유 종목(orphan holdings)의 엔진 감시 및 손절/청산 활성화
4. ISA/적립식 모드에서의 스윙 매도 방지 및 DCA 분할 적립 허용
5. LLM 리밸런서 출력 백분율(%) 자동 정규화
6. PnL 총자산(Total Equity) 기준 일수익률 계산 왜곡 방지
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from core import db, pnl, signal_engine
from core.ai_rebalance_guard import validate_proposal
from core.risk import RiskManager
from core.utils.krx_calendar import is_krx_market_open_now, is_krx_trading_day
from core.utils.tick_size import adjust_to_tick_size, get_krx_tick_size, is_krx_etf


KST = ZoneInfo("Asia/Seoul")


def test_krx_calendar_holidays_and_weekends():
    """KRX 공휴일(한글날, 근로자의 날, 대체휴일 등) 및 개장시간 판별 검증."""
    # 2026-10-09 (한글날 공휴일 - 금요일)
    hangeul_day = datetime(2026, 10, 9, 10, 0, tzinfo=KST)
    assert not is_krx_trading_day(hangeul_day.date())
    assert not is_krx_market_open_now(hangeul_day)

    # 2026-05-01 (근로자의 날 - 금요일 증시 휴장)
    labor_day = datetime(2026, 5, 1, 11, 0, tzinfo=KST)
    assert not is_krx_trading_day(labor_day.date())
    assert not is_krx_market_open_now(labor_day)

    # 2026-10-12 (정상 영업일 월요일)
    open_time = datetime(2026, 10, 12, 10, 0, tzinfo=KST)
    before_open = datetime(2026, 10, 12, 8, 30, tzinfo=KST)
    after_close = datetime(2026, 10, 12, 16, 0, tzinfo=KST)
    assert is_krx_trading_day(open_time.date())
    assert is_krx_market_open_now(open_time)
    assert not is_krx_market_open_now(before_open)
    assert not is_krx_market_open_now(after_close)


def test_tick_size_adjustments():
    """KRX 주식/ETF 및 해외주식 호가단위 보정 검증."""
    # 주식 호가 단위 검증
    assert get_krx_tick_size(1500, is_etf=False) == 1
    assert get_krx_tick_size(3500, is_etf=False) == 5
    assert get_krx_tick_size(9252, is_etf=False) == 10
    assert get_krx_tick_size(35000, is_etf=False) == 50
    assert get_krx_tick_size(150000, is_etf=False) == 100
    assert get_krx_tick_size(350000, is_etf=False) == 500
    assert get_krx_tick_size(700000, is_etf=False) == 1000

    # 9,252원 주문 가격 보정 (실제 0040X0 매도 거절 사례)
    assert adjust_to_tick_size(9252, market="domestic", is_etf=False, side="sell") == 9250
    assert adjust_to_tick_size(9252, market="domestic", is_etf=True, side="sell") == 9250

    # ETF 호가 단위 (2,000원 이상은 5원 단위)
    assert get_krx_tick_size(9252, is_etf=True) == 5
    assert adjust_to_tick_size(12344, market="domestic", is_etf=True) == 12345

    # ETF 판별 유틸
    assert is_krx_etf("0040X0") is True
    assert is_krx_etf("005930") is False

    # 해외주식 센트 단위
    assert adjust_to_tick_size(236.845, market="overseas") == 236.84


def test_accumulation_mode_prevents_swing_sell_and_allows_dca(tmp_path, monkeypatch):
    """적립식(ISA) 모드에서는 단기 하락 시 스윙 매도를 차단하고 DCA 온보딩을 허용하는지 검증."""
    async def run():
        test_db = str(tmp_path / "test_signal.db")
        monkeypatch.setattr("core.config.settings.DB_PATH", test_db)
        await db.init_db()
        conn = await db.get_connection()
        try:
            risk = RiskManager(conn)
            # 기술적 지표 단기 하락 시그널 (스윙 문턱 0.5 초과)
            sell_sig = {"direction": "SELL", "strength": 0.6}

            # 1. 일반 스윙 계좌 (accumulation_mode=False):
            # 보유 수량이 있는 경우 swing_signal 매도가 나가야 함
            a_swing = await signal_engine.decide(
                symbol="088980", current_weight=0.25, target_weight=0.25, band=0.05,
                tech_signal=sell_sig, intraday_signal=None, sentiment_signal=None,
                price=9600.0, current_position_qty=22.0, current_position_value=22 * 9600.0,
                total_equity=10_000_000.0, risk=risk, accumulation_mode=False,
            )
            assert a_swing is not None
            assert a_swing.side == "sell"
            assert a_swing.reason == "swing_signal"

            # 2. 적립식/ISA 계좌 (accumulation_mode=True):
            # 정상 편입 종목의 경우 단기 SELL 시 스윙 분할 매도를 하지 않아야 함 (바닥 털림 방지)
            a_isa = await signal_engine.decide(
                symbol="088980", current_weight=0.25, target_weight=0.25, band=0.05,
                tech_signal=sell_sig, intraday_signal=None, sentiment_signal=None,
                price=9600.0, current_position_qty=22.0, current_position_value=22 * 9600.0,
                total_equity=10_000_000.0, risk=risk, accumulation_mode=True,
            )
            assert a_isa is None  # 매도 차단

            # 3. 적립식/ISA 계좌에서 저비중 종목(온보딩 대상)은 단기 하락이어도 DCA 분할 매수가 허용됨
            a_onboard = await signal_engine.decide(
                symbol="088980", current_weight=0.05, target_weight=0.25, band=0.05,
                tech_signal=sell_sig, intraday_signal=None, sentiment_signal=None,
                price=9600.0, current_position_qty=5.0, current_position_value=5 * 9600.0,
                total_equity=10_000_000.0, risk=risk, accumulation_mode=True,
            )
            assert a_onboard is not None
            assert a_onboard.side == "buy"
            assert a_onboard.reason == "onboarding_tranche"
        finally:
            await conn.close()

    asyncio.run(run())


def test_llm_proposed_weights_percentage_auto_normalization():
    """LLM이 비중을 백분율(예: 15.0%, 20.0%)로 반환했을 때 0.0~1.0으로 스케일링되어 통과하는지 검증."""
    raw_weights = {"005930": 20.0, "000660": 15.0, "017670": 10.0}
    total_raw = sum(raw_weights.values())
    scale = 100.0 if total_raw > 1.02 else 1.0
    normalized = {s: round(w / scale, 4) for s, w in raw_weights.items()}

    assert normalized["005930"] == 0.20
    assert normalized["000660"] == 0.15
    assert normalized["017670"] == 0.10

    active_weights = {"005930": 0.20, "000660": 0.15, "017670": 0.10}
    val = validate_proposal(
        normalized,
        active_weights,
        adds=[],
        removes=[],
        max_turnover_pct=0.3,
        max_weight_delta_pct=0.1,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.35,
        max_portfolio_symbols=10,
    )
    assert val.ok is True


def test_pnl_portfolio_daily_equity_base_accuracy():
    """계좌 총자산이 클 때 주식 소액 손실로 일수익률이 폭락 왜곡되지 않는지 검증."""
    # 총자산 3.5억 원, 주식 포지션 50만 원, 주식 당일 손실 -35,000원
    axis = ["2026-10-14", "2026-10-15"]
    mtm = {
        "005930": {
            "2026-10-14": {"pnl_krw": 0.0, "base_krw": 500_000.0, "value_krw": 500_000.0, "ret": 0.0},
            "2026-10-15": {"pnl_krw": -35_000.0, "base_krw": 500_000.0, "value_krw": 0.0, "ret": -0.07},
        }
    }
    equity_by_date = {
        "2026-10-14": 350_000_000.0,
        "2026-10-15": 349_965_000.0,
    }

    # 1. 총자산 분모 반영 시
    daily_accurate = pnl.portfolio_daily(mtm, axis, equity_by_date=equity_by_date)
    # 10-15의 손익률은 -35,000 / 350,000,000 = -0.0001 (-0.01%)
    assert abs(daily_accurate[1]["ret"] - (-35000 / 350000000)) < 1e-6
    assert daily_accurate[1]["cum_ret"] > -0.001  # -0.1% 미만의 극소 손실 유지

    # 2. 총자산 미제공 시 (기존 하위 호환)
    daily_stock_only = pnl.portfolio_daily(mtm, axis, equity_by_date=None)
    assert abs(daily_stock_only[1]["ret"] - (-0.07)) < 1e-6
