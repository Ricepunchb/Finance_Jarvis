#!/usr/bin/env python3
"""Finance Jarvis 실전투자 전환을 위한 모의투자 데이터 백업 및 운영 테이블 클린 초기화 스크립트.

수행 작업:
1. 기존 data/jarvis.db 전체를 data/jarvis_mock_backup_YYYYMMDD_HHMMSS.db 로 백업 보존
2. 모의투자 매매 이력(fills, order_intents, positions, decision_log, rebalance_events 등) 안전 삭제
3. 실전 계좌에 불필요한 캐시 및 상태 리셋
4. 종목 마스터(stock_master, stock_theme), 발굴 후보(candidate_universe), 백테스트 바(backtest_bars)는 완전 보존
5. 포트폴리오 종목(portfolio_symbols, active_target_weights) 유지 또는 초기화 선택
"""
import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "jarvis.db"


def parse_args():
    parser = argparse.ArgumentParser(description="실전투자 전환용 DB 클린 리셋")
    parser.add_argument(
        "--reset-portfolio",
        action="store_true",
        help="portfolio_symbols 및 target_weights도 기본 초기값으로 리셋 (미지정 시 현재 구성 유지)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not DB_PATH.exists():
        print(f"❌ DB 파일이 존재하지 않습니다: {DB_PATH}")
        sys.exit(1)

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = ROOT / "data" / f"jarvis_mock_backup_{now_str}.db"

    print("=========================================================================================")
    print("🚀 Finance Jarvis 실전투자 전환 데이터 초기화")
    print("=========================================================================================")

    # 1. 원본 DB 백업
    print(f"\n1️⃣ [모의투자 DB 전체 백업]")
    shutil.copy2(DB_PATH, backup_path)
    print(f"  · 백업 완료: {backup_path.name} ({backup_path.stat().st_size / 1024 / 1024:.2f} MB)")

    # 2. 운영/매매 테이블 초기화
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    trade_tables = [
        "fills",
        "order_intents",
        "positions",
        "decision_log",
        "rebalance_events",
        "cycles",
        "intraday_bars",
        "dividends",
        "symbol_thesis",
    ]

    print(f"\n2️⃣ [모의투자 매매 및 운영 테이블 정리]")
    for table in trade_tables:
        cursor.execute(f"DELETE FROM {table}")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        print(f"  · {table:<20}: 초기화 완료 (0건)")

    # 엔진 상태 테이블 중 모의투자 실행 플래그 정리
    cursor.execute("DELETE FROM engine_state WHERE key NOT LIKE 'discovery_%'")
    print(f"  · {'engine_state':<20}: 엔진 런타임 상태 초기화")

    # 3. 포트폴리오 구성 처리
    print(f"\n3️⃣ [포트폴리오 종목 및 목표 비중 설정]")
    if args.reset_portfolio:
        cursor.execute("DELETE FROM portfolio_symbols")
        cursor.execute("DELETE FROM target_weights")
        cursor.execute("DELETE FROM active_target_weights")
        # 기본 3개 주력 종목 등록
        default_symbols = [
            ("005930", "domestic", None, time.time(), 1),
            ("000660", "domestic", None, time.time(), 1),
            ("017670", "domestic", None, time.time(), 1),
        ]
        cursor.executemany(
            "INSERT INTO portfolio_symbols(symbol, market, exchange, added_at, enabled) VALUES (?, ?, ?, ?, ?)",
            default_symbols,
        )
        for s in ["005930", "000660", "017670"]:
            cursor.execute(
                "INSERT INTO active_target_weights(symbol, weight, approved_at, source_proposal_id) VALUES (?, ?, ?, ?)",
                (s, 0.333, time.time(), 0),
            )
        print("  · 기본 3종목 (005930 삼전, 000660 하닉, 017670 SKT) 균등비중(각 33.3%)으로 초기화 완료")
    else:
        # 기존 등록된 종목 유지하되 비활성/제외 처리된 것 정리
        cursor.execute("DELETE FROM portfolio_symbols WHERE enabled = 0")
        held_syms = [r[0] for r in cursor.execute("SELECT symbol FROM portfolio_symbols").fetchall()]
        print(f"  · 기존 활성 포트폴리오 종목 유지: {held_syms} (총 {len(held_syms)}종목)")

    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    print("\n=========================================================================================")
    print("🎉 실전투자용 클린 리셋이 완료되었습니다!")
    print(f"· 보존된 백업 파일: {backup_path}")
    print("· 이제 tmux 세션을 재시작하시면 실전 계좌에서 0부터 깨끗하게 시작됩니다.")
    print("=========================================================================================\n")


if __name__ == "__main__":
    main()

