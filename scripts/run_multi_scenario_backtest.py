#!/usr/bin/env python3
"""Finance Jarvis 다중 시나리오 백테스팅 및 강건성(Robustness) 검증 스크립트.

다양한 시장 국면, 유니버스 조합, 벤치마크, 거래비용 스트레스 환경에서
Jarvis 전략의 베이스라인 대비 초과 성과 및 리스크 방어력을 전방위로 검증합니다.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from core import db
from core.backtest import BacktestConfig, BacktestRunner
from core.kis_client import AsyncKISClient


SCENARIOS = [
    # 1. 시장 국면별 시나리오 (기본 유니버스: 삼성전자, SK하이닉스, SK텔레콤)
    {
        "id": "SCEN_1_FULL_YEAR",
        "name": "최근 1년 장기 (2025-10-01 ~ 2026-10-01)",
        "symbols": ["005930", "000660", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",  # S&P 500 ETF
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    {
        "id": "SCEN_2_BULL_RALLY",
        "name": "2026 상반기 랠리 국면 (2026-01-01 ~ 2026-05-31)",
        "symbols": ["005930", "000660", "017670"],
        "start": "2026-01-01",
        "end": "2026-05-31",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    {
        "id": "SCEN_3_CHOPPY_CORRECTION",
        "name": "2026 여름/가을 조정 국면 (2026-06-01 ~ 2026-10-01)",
        "symbols": ["005930", "000660", "017670"],
        "start": "2026-06-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    # 2. 국내 대표 지수(KOSPI 226490) 벤치마크 대비 검증
    {
        "id": "SCEN_4_VS_KOSPI",
        "name": "KOSPI ETF(226490) 대비 성과 비교",
        "symbols": ["005930", "000660", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "226490",  # KODEX 코스피
        "bench_name": "KODEX 코스피",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    # 3. 유니버스 다변화 시나리오
    {
        "id": "SCEN_5_TECH_FOCUS",
        "name": "반도체/테크 집중 (005930, 000660)",
        "symbols": ["005930", "000660"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    {
        "id": "SCEN_6_VALUE_CYCLICAL",
        "name": "가치/경기민감/금융 혼합 (005380 현대차, 006800 미래에셋, 017670 SKT)",
        "symbols": ["005380", "006800", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    {
        "id": "SCEN_7_DIVERSIFIED_4",
        "name": "4종목 균형 분산 (삼전, 하닉, 현대차, SKT)",
        "symbols": ["005930", "000660", "005380", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0005,
    },
    # 4. 가혹 환경 스트레스 테스트
    {
        "id": "SCEN_8_HARSH_COST",
        "name": "가혹 거래비용 (슬리피지 2배, 수수료 2배)",
        "symbols": ["005930", "000660", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.06,
        "slippage": 0.0010,  # 0.10% (기본의 2배)
        "fee_rate": 0.00030,  # 0.03% (기본의 2배)
    },
    {
        "id": "SCEN_9_TIGHT_STOP_LOSS",
        "name": "타이트한 손절 (-5% STOP_LOSS)",
        "symbols": ["005930", "000660", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.05,
        "slippage": 0.0005,
    },
    {
        "id": "SCEN_10_LOOSE_STOP_LOSS",
        "name": "완화된 손절 (-8% STOP_LOSS)",
        "symbols": ["005930", "000660", "017670"],
        "start": "2025-10-01",
        "end": "2026-10-01",
        "benchmark": "360750",
        "bench_name": "S&P 500 ETF",
        "stop_loss": 0.08,
        "slippage": 0.0005,
    },
]


def calculate_buy_and_hold_return(runner: BacktestRunner, symbols: List[str], start_date: str, end_date: str) -> float:
    """동일 기간 대상 종목들의 균등 매수 후 보유(Buy & Hold) 수익률 산출."""
    total_ret = 0.0
    valid_count = 0
    for sym in symbols:
        # data_manager 캐시에서 확인
        df = runner.data_manager.conn  # fallback
    return 0.0


async def main():
    await db.init_db()
    conn = await db.get_connection()
    # KIS 클라이언트 초기화 없이 캐시된 과거 데이터로만 우선 초고속 실행 가능
    # (필요 시 KIS client 전달)
    from core.engine import TradingEngine
    engine_inst = TradingEngine()
    runner = BacktestRunner(conn, client=engine_inst.client)

    results = []

    print("=========================================================================================")
    print("📊 Finance Jarvis 다중 시나리오 백테스팅 및 Robustness 검증")
    print("=========================================================================================\n")

    for sc in SCENARIOS:
        print(f"▶ 실행 중: [{sc['id']}] {sc['name']} ...")
        config = BacktestConfig(
            symbols=sc["symbols"],
            start_date=sc["start"],
            end_date=sc["end"],
            initial_cash=10_000_000.0,
            benchmark_symbol=sc["benchmark"],
            stop_loss_pct=sc.get("stop_loss", 0.06),
            slippage_pct=sc.get("slippage", 0.0005),
            fee_rate=sc.get("fee_rate", 0.00015),
        )

        res = await runner.run(config)
        metrics = res.get("metrics", {})
        strat = metrics.get("strategy", {})
        bench = metrics.get("benchmark", {})
        rel = metrics.get("relative", {})
        trades = metrics.get("trade_stats", {})

        s_ret = strat.get("total_return_pct", 0.0)
        b_ret = bench.get("benchmark_return_pct", 0.0)
        excess = rel.get("excess_return_pct", 0.0)
        s_mdd = strat.get("mdd_pct", 0.0)
        b_mdd = bench.get("benchmark_mdd_pct", 0.0)
        s_sharpe = strat.get("sharpe", 0.0)
        b_sharpe = bench.get("benchmark_sharpe", 0.0)
        win_rate = trades.get("win_rate_pct", 0.0)
        pf = trades.get("profit_factor", 0.0)
        sl_cnt = trades.get("stop_loss_count", 0)
        ts_cnt = trades.get("trailing_stop_count", 0)
        tot_trades = trades.get("total_trades", 0)

        results.append({
            "id": sc["id"],
            "name": sc["name"],
            "period": f"{sc['start']} ~ {sc['end']}",
            "benchmark": sc["bench_name"],
            "strat_ret": s_ret,
            "bench_ret": b_ret,
            "excess_ret": excess,
            "strat_mdd": s_mdd,
            "bench_mdd": b_mdd,
            "mdd_improvement": round(b_mdd - s_mdd, 2) if (b_mdd and s_mdd) else 0.0,
            "strat_sharpe": s_sharpe,
            "bench_sharpe": b_sharpe,
            "win_rate": win_rate,
            "profit_factor": pf,
            "total_trades": tot_trades,
            "stop_loss_cnt": sl_cnt,
            "trailing_cnt": ts_cnt,
            "outperform": s_ret > b_ret,
        })

    await conn.close()

    print("\n" + "=" * 120)
    print("📋 [다중 시나리오 백테스트 종합 비교 결과표]")
    print("=" * 120)
    header = (
        f"{'시나리오':<32} | {'전략 수익률':<10} | {'벤치마크':<10} | {'초과수익(α)':<10} | "
        f"{'전략 MDD':<9} | {'벤치 MDD':<9} | {'샤프(전/벤)':<11} | {'승률':<7} | {'손익비':<6} | {'우위'}"
    )
    print(header)
    print("-" * 120)

    total_excess = 0.0
    outperform_count = 0
    mdd_defense_count = 0

    for r in results:
        status = "✅ 승리" if r["outperform"] else "❌ 열위"
        if r["outperform"]:
            outperform_count += 1
        if abs(r["strat_mdd"]) <= abs(r["bench_mdd"]):
            mdd_defense_count += 1
        total_excess += r["excess_ret"]

        line = (
            f"{r['name']:<30} | "
            f"{r['strat_ret']:>9.2f}% | "
            f"{r['bench_ret']:>9.2f}% | "
            f"{r['excess_ret']:>+9.2f}%p | "
            f"{r['strat_mdd']:>8.2f}% | "
            f"{r['bench_mdd']:>8.2f}% | "
            f"{r['strat_sharpe']:>4.2f}/{r['bench_sharpe']:>4.2f} | "
            f"{r['win_rate']:>6.1f}% | "
            f"{r['profit_factor']:>6.2f} | "
            f"{status}"
        )
        print(line)

    print("-" * 120)
    avg_excess = total_excess / len(results)
    print(f"🎯 종합 결과:")
    print(f"· 벤치마크 초과 수익률(Alpha) 승률: {outperform_count}/{len(results)} ({outperform_count/len(results)*100:.1f}%)")
    print(f"· 최대 낙폭(MDD) 방어 성공률: {mdd_defense_count}/{len(results)} ({mdd_defense_count/len(results)*100:.1f}%)")
    print(f"· 전 시나리오 평균 초과 수익률: {avg_excess:+.2f}%p\n")

    # JSON 저장
    out_path = ROOT / "data" / "multi_scenario_backtest_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"💾 상세 결과가 '{out_path}'에 저장되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())

