#!/usr/bin/env python3
"""Finance Jarvis 백테스팅 CLI 실행기.

사용 예시:
  uv run python scripts/run_backtest.py --symbols 005930,000660,017670 --start 2025-01-01 --end 2026-09-30 --benchmark 360750
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db
from core.backtest import BacktestConfig, BacktestRunner
from core.config import settings
from core.engine import TradingEngine


def parse_args():
    parser = argparse.ArgumentParser(description="Finance Jarvis 전략 백테스팅")
    parser.add_argument(
        "--symbols",
        type=str,
        default="005930,000660,017670",
        help="대상 종목코드 쉼표 구분 (예: 005930,000660)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="최근 N일간 백테스트 (start/end 대신 사용 가능)",
    )
    parser.add_argument("--start", type=str, default="", help="시작일 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="", help="종료일 (YYYY-MM-DD)")
    parser.add_argument(
        "--benchmark",
        type=str,
        default=settings.BACKTEST_DEFAULT_BENCHMARK,
        help="벤치마크 종목 (기본: 360750 - TIGER 미국S&P500)",
    )
    parser.add_argument(
        "--cash",
        type=float,
        default=10_000_000.0,
        help="초기 자본금 (원, 기본: 10,000,000)",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=settings.STOP_LOSS_PCT,
        help=f"손절 기준 (기본: {settings.STOP_LOSS_PCT})",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=settings.STOP_LOSS_COOLDOWN_DAYS,
        help=f"손절 후 재진입 쿨다운 일수 (기본: {settings.STOP_LOSS_COOLDOWN_DAYS})",
    )
    parser.add_argument(
        "--onboarding-days",
        type=int,
        default=settings.ONBOARDING_DAYS,
        help=f"분할 온보딩 일수 (기본: {settings.ONBOARDING_DAYS})",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="결과 JSON 저장 경로 (선택)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("❌ 대상 종목을 최소 1개 이상 지정해야 합니다.")
        sys.exit(1)

    today = datetime.now().date()
    end_date = args.end if args.end else today.strftime("%Y-%m-%d")
    if args.start:
        start_date = args.start
    else:
        start_date = (today - timedelta(days=args.days)).strftime("%Y-%m-%d")

    config = BacktestConfig(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        initial_cash=args.cash,
        benchmark_symbol=args.benchmark,
        stop_loss_pct=args.stop_loss,
        stop_loss_cooldown_days=args.cooldown,
        onboarding_days=args.onboarding_days,
    )

    print(f"\n=======================================================")
    print(f"🚀 Finance Jarvis 전략 백테스팅")
    print(f"=======================================================")
    print(f"· 기간: {start_date} ~ {end_date}")
    print(f"· 대상 종목: {', '.join(symbols)}")
    print(f"· 벤치마크: {args.benchmark} (S&P 500 ETF)")
    print(f"· 초기 자본금: {args.cash:,.0f}원")
    print(f"-------------------------------------------------------\n")

    await db.init_db()
    conn = await db.get_connection()
    engine_inst = TradingEngine()

    try:
        runner = BacktestRunner(conn, client=engine_inst.client)
        print("⏳ 과거 데이터 로드 및 시뮬레이션 중...")
        res = await runner.run(config)
    finally:
        await conn.close()

    metrics = res.get("metrics", {})
    strat = metrics.get("strategy", {})
    bench = metrics.get("benchmark", {})
    rel = metrics.get("relative", {})
    trades = metrics.get("trade_stats", {})

    print("\n📊 [성과 요약: Jarvis 전략 vs S&P 500 ETF]")
    print(f"{'항목':<22} | {'Jarvis 전략':<15} | {'S&P 500 ETF (' + args.benchmark + ')':<15}")
    print("-" * 60)
    print(f"{'누적 수익률 (Total Return)':<18} | {strat.get('total_return_pct', 0.0):>13.2f}% | {bench.get('benchmark_return_pct', 0.0):>13.2f}%")
    print(f"{'연평균 복리 (CAGR)':<22} | {strat.get('cagr_pct', 0.0):>13.2f}% | {bench.get('benchmark_cagr_pct', 0.0):>13.2f}%")
    print(f"{'최대 낙폭 (MDD)':<23} | {strat.get('mdd_pct', 0.0):>13.2f}% | {bench.get('benchmark_mdd_pct', 0.0):>13.2f}%")
    print(f"{'연율화 변동성 (Vol)':<21} | {strat.get('volatility_pct', 0.0):>13.2f}% | {bench.get('benchmark_volatility_pct', 0.0):>13.2f}%")
    print(f"{'샤프 지수 (Sharpe)':<23} | {strat.get('sharpe', 0.0):>14.2f} | {bench.get('benchmark_sharpe', 0.0):>14.2f}")
    print(f"{'소티노 지수 (Sortino)':<21} | {strat.get('sortino', 0.0):>14.2f} | {'-':>14}")

    print("\n🎯 [상대 성과 및 리스크 분석]")
    print(f"· 초과 수익률 (Alpha vs ETF): {rel.get('excess_return_pct', 0.0):+.2f}%p")
    print(f"· 벤치마크 대비 베타 (Beta): {rel.get('beta', 0.0) if rel.get('beta') is not None else 'N/A'}")
    print(f"· 젠센의 알파 (Annualized): {rel.get('alpha_pct', 0.0) if rel.get('alpha_pct') is not None else 'N/A'}%")

    print("\n📝 [매매 및 안전장치 통계]")
    print(f"· 총 매매 건수: {trades.get('total_trades', 0)}건 (매수 {trades.get('buy_trades', 0)}건, 매도 {trades.get('sell_trades', 0)}건)")
    print(f"· 승률 (Win Rate): {trades.get('win_rate_pct', 0.0)}% (수익 {trades.get('win_count', 0)}건 / 손실 {trades.get('loss_count', 0)}건)")
    print(f"· 손익비 (Profit Factor): {trades.get('profit_factor', 0.0)}")
    print(f"· 손절 발동 (-7% STOP_LOSS): {trades.get('stop_loss_count', 0)}회")
    print(f"· ATR 트레일링 익절 발동: {trades.get('trailing_stop_count', 0)}회")
    print(f"· 실현 총손익: {trades.get('total_realized_pnl', 0.0):+,.0f}원")
    print(f"· 수수료 및 거래세 합계: {trades.get('total_fees_and_taxes', 0.0):,.0f}원")
    print(f"· 최종 자산: {strat.get('final_equity', args.cash):,.0f}원\n")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"💾 전체 결과가 '{args.out}'에 저장되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
