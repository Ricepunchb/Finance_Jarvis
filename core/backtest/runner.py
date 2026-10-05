# core/backtest/runner.py
"""백테스팅 오케스트레이터.

설정(BacktestConfig)을 받아 데이터 수집, 시뮬레이션, 지표 산출,
UI 및 API 응답용 포맷팅을 일괄 처리합니다.
"""
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import aiosqlite
import pandas as pd
from pydantic import BaseModel, Field

from core.backtest.data import HistoricalDataManager
from core.backtest.engine import BacktestEngine, BacktestResult
from core.backtest.metrics import calculate_backtest_metrics
from core.config import settings
from core.kis_client import AsyncKISClient


class BacktestConfig(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: ["005930", "000660", "017670"])
    start_date: str = "2025-01-01"
    end_date: str = "2026-09-30"
    initial_cash: float = 10_000_000.0
    benchmark_symbol: str = settings.BACKTEST_DEFAULT_BENCHMARK  # 기본: 360750 (TIGER 미국S&P500)
    benchmark_market: str = "domestic"
    benchmark_exchange: Optional[str] = None
    target_weights: Optional[Dict[str, float]] = None
    fee_rate: float = settings.BACKTEST_DEFAULT_FEE_PCT
    tax_rate: float = settings.BACKTEST_DEFAULT_TAX_PCT
    slippage_pct: float = settings.BACKTEST_DEFAULT_SLIPPAGE_PCT
    stop_loss_pct: float = settings.STOP_LOSS_PCT
    stop_loss_cooldown_days: int = settings.STOP_LOSS_COOLDOWN_DAYS
    onboarding_days: int = settings.ONBOARDING_DAYS
    require_uptrend_for_onboarding: bool = settings.REQUIRE_UPTREND_FOR_ONBOARDING
    require_sma20_for_buy: bool = settings.REQUIRE_SMA20_FOR_BUY


class BacktestRunner:
    def __init__(self, conn: aiosqlite.Connection, client: Optional[AsyncKISClient] = None):
        self.conn = conn
        self.client = client
        self.data_manager = HistoricalDataManager(conn, client)

    async def run(self, config: BacktestConfig) -> Dict[str, Any]:
        """백테스트 전체 파이프라인 실행."""
        # 1. 포트폴리오 종목 및 벤치마크 데이터 로드
        data_by_symbol: Dict[str, pd.DataFrame] = {}
        for sym in config.symbols:
            df = await self.data_manager.get_ohlcv(
                sym, config.start_date, config.end_date, market="domestic", include_padding=True
            )
            data_by_symbol[sym] = df

        # 벤치마크 종목 로드
        bench_df = await self.data_manager.get_ohlcv(
            config.benchmark_symbol,
            config.start_date,
            config.end_date,
            market=config.benchmark_market,
            exchange=config.benchmark_exchange,
            include_padding=False,
        )

        # 2. 백테스트 시뮬레이션 실행
        engine = BacktestEngine(
            symbols=config.symbols,
            data_by_symbol=data_by_symbol,
            initial_cash=config.initial_cash,
            target_weights=config.target_weights,
            fee_rate=config.fee_rate,
            tax_rate=config.tax_rate,
            slippage_pct=config.slippage_pct,
            stop_loss_pct=config.stop_loss_pct,
            stop_loss_cooldown_days=config.stop_loss_cooldown_days,
            onboarding_days=config.onboarding_days,
            require_uptrend_for_onboarding=config.require_uptrend_for_onboarding,
            require_sma20_for_buy=config.require_sma20_for_buy,
        )
        result: BacktestResult = engine.run(config.start_date, config.end_date)

        # 3. 성과 및 비교 메트릭 계산
        metrics = calculate_backtest_metrics(result, bench_df)

        # 4. 차트 및 시각화용 시계열 생성
        chart_data = self._build_chart_data(result, bench_df, config.initial_cash)

        # 5. 매매일지 직렬화
        trades_list = [
            {
                "symbol": t.symbol,
                "date": t.date,
                "side": t.side,
                "price": round(t.price, 1),
                "qty": t.qty,
                "notional": round(t.notional, 0),
                "fee": round(t.fee, 0),
                "tax": round(t.tax, 0),
                "reason": t.reason,
                "realized_pnl": round(t.realized_pnl, 0),
                "return_pct": round(t.return_pct, 2),
            }
            for t in result.trades
        ]

        return {
            "config": config.model_dump(),
            "metrics": metrics,
            "chart_data": chart_data,
            "trades": trades_list,
        }

    def _build_chart_data(
        self, result: BacktestResult, bench_df: Optional[pd.DataFrame], initial_cash: float
    ) -> List[Dict[str, Any]]:
        """Plotly 차트용 시계열 데이터 구성 (전략 vs 벤치마크 정규화 자산)."""
        if not result.daily_snapshots:
            return []

        bench_map = {}
        first_bench_price = None
        if bench_df is not None and not bench_df.empty:
            df_sorted = bench_df.sort_values("date").reset_index(drop=True)
            for _, r in df_sorted.iterrows():
                d_str = r["date"].strftime("%Y-%m-%d")
                c_val = float(r["close"])
                bench_map[d_str] = c_val

        rows: List[Dict[str, Any]] = []
        running_max_strat = initial_cash
        running_max_bench = initial_cash

        for s in result.daily_snapshots:
            strat_equity = s.total_equity
            running_max_strat = max(running_max_strat, strat_equity)
            strat_dd = ((strat_equity - running_max_strat) / running_max_strat) * 100.0

            # 벤치마크 정규화 자산 계산 (초기자본 기준 매수 후 보유)
            bench_price = bench_map.get(s.date)
            bench_equity = None
            bench_dd = None

            if bench_price is not None and bench_price > 0:
                if first_bench_price is None:
                    first_bench_price = bench_price
                bench_equity = initial_cash * (bench_price / first_bench_price)
                running_max_bench = max(running_max_bench, bench_equity)
                bench_dd = ((bench_equity - running_max_bench) / running_max_bench) * 100.0

            rows.append({
                "date": s.date,
                "strategy_equity": round(strat_equity, 0),
                "strategy_drawdown": round(strat_dd, 2),
                "cash": round(s.cash, 0),
                "benchmark_equity": round(bench_equity, 0) if bench_equity is not None else None,
                "benchmark_drawdown": round(bench_dd, 2) if bench_dd is not None else None,
                "daily_return": round(s.daily_return * 100.0, 2),
            })

        return rows
