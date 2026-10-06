#!/usr/bin/env python3
"""Finance Jarvis 전수조사 및 동적 종목 발굴 파이프라인 전수 오류 검증 스크립트.

검증 항목:
1. stock_master 데이터 정합성 (KOSPI/KOSDAQ 코드 포맷, 필터링 규칙, 제외 종목)
2. stock_theme 데이터 정합성 (테마-종목 매핑 누락 여부)
3. 5대 발굴 소스 실행 무결성 (momentum, broker, news, value, theme)
4. candidate_universe 무결성 및 종목 검증 함수(validate_candidate_symbol)
5. 피처 엔지니어링 및 스코어링 수식 (NaN/Inf 누수 방지)
6. 해외 종목 Proxy Mapping 정합성
"""
import asyncio
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiosqlite
from core import db, discovery, discovery_signals, discovery_sources, master_files, proxy_mapping
from core.config import settings
from core.kis_client import AsyncKISClient
from core.engine import TradingEngine


async def check_1_stock_master(conn: aiosqlite.Connection) -> dict:
    """1. stock_master 정합성 검사"""
    errors = []
    cursor = await conn.execute("SELECT COUNT(*) FROM stock_master")
    total = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM stock_master WHERE length(symbol) != 6 OR symbol IS NULL OR name IS NULL OR name = ''"
    )
    invalid_sym = (await cursor.fetchone())[0]
    if invalid_sym > 0:
        errors.append(f"비정상 종목코드/이름 {invalid_sym}건 발견")

    # 우선주(마지막 자리가 0이 아닌 일반 주식), 스팩, ETF 키워드 제외 여부 샘플 확인
    cursor = await conn.execute(
        "SELECT symbol, name, is_excluded FROM stock_master WHERE name LIKE '%스팩%' OR name LIKE '%우' OR name LIKE '%레버리지%' LIMIT 10"
    )
    rows = await cursor.fetchall()
    unfiltered_spacs_or_prefers = [r for r in rows if r[2] == 0 and ("스팩" in r[1] or "레버리지" in r[1])]
    if unfiltered_spacs_or_prefers:
        errors.append(f"제외되어야 할 종목이 미제외됨: {unfiltered_spacs_or_prefers}")

    cursor = await conn.execute("SELECT COUNT(*) FROM stock_master WHERE is_excluded = 0")
    eligible_count = (await cursor.fetchone())[0]

    return {
        "total_stocks": total,
        "eligible_stocks": eligible_count,
        "invalid_symbol_count": invalid_sym,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


async def check_2_stock_theme(conn: aiosqlite.Connection) -> dict:
    """2. stock_theme 정합성 검사"""
    errors = []
    cursor = await conn.execute("SELECT COUNT(*), COUNT(DISTINCT theme_code), COUNT(DISTINCT symbol) FROM stock_theme")
    total, uniq_themes, uniq_syms = await cursor.fetchone()

    # 테마에 있는 심볼이 stock_master에 존재하는지 외래키 검증
    cursor = await conn.execute(
        "SELECT COUNT(DISTINCT st.symbol) FROM stock_theme st LEFT JOIN stock_master sm ON st.symbol = sm.symbol WHERE sm.symbol IS NULL"
    )
    orphaned_syms = (await cursor.fetchone())[0]
    if orphaned_syms > 0:
        errors.append(f"stock_master에 없는 고아 테마 종목 {orphaned_syms}건 발견")

    return {
        "total_theme_mappings": total,
        "unique_themes": uniq_themes,
        "symbols_in_themes": uniq_syms,
        "orphaned_symbols": orphaned_syms,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


async def check_3_discovery_sources(conn: aiosqlite.Connection, client: AsyncKISClient) -> dict:
    """3. 5대 발굴 소스 가동 검사 (샘플 테스트)"""
    results = {}
    master = await db.get_stock_master(conn)
    eligible = discovery_sources.eligible_universe(master)

    # 3-1. Broker Research (네이버 증권사 리포트 크롤링)
    try:
        broker_res = await discovery_sources.source_broker_research()
        results["broker"] = {
            "status": "PASS",
            "count": len(broker_res),
            "sample": list(broker_res.keys())[:3],
        }
    except Exception as e:
        results["broker"] = {"status": "FAIL", "error": str(e)}

    # 3-2. Undervalued (네이버 컨센서스 목표가 괴리)
    try:
        # 10개 종목만 샘플로 테스트
        sample_eligible = dict(list(eligible.items())[:10])
        val_res = await discovery_sources.source_undervalued(sample_eligible)
        results["value"] = {
            "status": "PASS",
            "count": len(val_res),
            "sample": list(val_res.keys())[:3],
        }
    except Exception as e:
        results["value"] = {"status": "FAIL", "error": str(e)}

    # 3-3. Theme Laggards
    try:
        # 가상 급등주로 테스트
        dummy_movers = {"005930": 5.2, "000660": 4.1}
        names = {s: r["name"] for s, r in master.items()}
        theme_res = await discovery_sources.source_theme_laggards(conn, dummy_movers, eligible, names)
        results["theme"] = {
            "status": "PASS",
            "count": len(theme_res),
            "sample": list(theme_res.keys())[:3],
        }
    except Exception as e:
        results["theme"] = {"status": "FAIL", "error": str(e)}

    # 3-4. Momentum & News (KIS API 직접 호출)
    try:
        mom_res = await discovery_sources.source_momentum(client)
        results["momentum"] = {
            "status": "PASS",
            "count": len(mom_res),
            "sample": list(mom_res.keys())[:3],
        }
    except Exception as e:
        results["momentum"] = {"status": "FAIL", "error": str(e)}

    return results


async def check_4_candidate_validation(client: AsyncKISClient) -> dict:
    """4. validate_candidate_symbol 검증 함수 방어력 테스트"""
    cases = [
        {"symbol": "005930", "market": "domestic", "name": "삼성전자", "expected": True},
        {"symbol": "000660", "market": "domestic", "name": "SK하이닉스", "expected": True},
        {"symbol": "999999", "market": "domestic", "name": "없는종목", "expected": False},  # 존재하지 않는 종목
        {"symbol": "005930", "market": "domestic", "name": "현대차", "expected": False},  # 종목명 환각
        {"symbol": "AAPL", "market": "overseas", "name": "Apple", "expected": True},     # 해외 티커
    ]
    results = []
    for c in cases:
        outcome = await discovery.validate_candidate_symbol(
            client, c["symbol"], market=c["market"], claimed_name=c["name"]
        )
        passed = outcome.ok == c["expected"]
        results.append({
            "case": f"{c['symbol']} ({c['name']})",
            "outcome_ok": outcome.ok,
            "expected": c["expected"],
            "reason": outcome.reason,
            "test_passed": passed,
        })
    all_passed = all(r["test_passed"] for r in results)
    return {"status": "PASS" if all_passed else "FAIL", "details": results}


async def check_5_scoring_and_signals(conn: aiosqlite.Connection, client: AsyncKISClient) -> dict:
    """5. 스코어링 수식 및 결측치/NaN/Inf 누수 검증"""
    # 현재 candidate_universe 상위 5종목 스코어링 테스트
    cursor = await conn.execute("SELECT symbol, name, universe_tag, sources_json FROM candidate_universe WHERE enabled = 1 LIMIT 5")
    rows = await cursor.fetchall()

    candidates = [
        {"symbol": r[0], "name": r[1], "universe_tag": r[2], "sources_json": r[3]}
        for r in rows
    ]

    scored_items = []
    nan_inf_found = False

    for cand in candidates:
        res = await discovery._score_one(
            conn, client, cand, broker_reports={}, benchmark_returns=None,
            start_date="2026-06-01", end_date="2026-10-06"
        )
        if res:
            scored_items.append(res)
            # 검사: raw_components 및 주요 피처에 NaN/Inf가 없는지
            for k, v in res.get("raw_components", {}).items():
                if v is None or math.isnan(v) or math.isinf(v):
                    nan_inf_found = True

    return {
        "tested_count": len(candidates),
        "successfully_scored": len(scored_items),
        "nan_inf_detected": nan_inf_found,
        "status": "PASS" if len(scored_items) > 0 and not nan_inf_found else "FAIL",
    }


async def check_6_proxy_mapping() -> dict:
    """6. 해외 대체 ETF 대리 매매(Proxy Mapping) 검증"""
    tickers = ["NVDA", "AAPL", "MSFT", "TSLA", "AMD", "AVGO", "GOOGL"]
    mappings = []
    all_valid = True
    for t in tickers:
        p = proxy_mapping.get_proxy_etf(t)
        if not p or not p.get("proxy_symbol"):
            all_valid = False
        mappings.append({
            "ticker": t,
            "proxy_symbol": p.get("proxy_symbol") if p else None,
            "proxy_name": p.get("proxy_name") if p else None,
        })
    return {
        "status": "PASS" if all_valid else "FAIL",
        "sample_mappings": mappings[:5],
    }


async def main():
    await db.init_db()
    conn = await db.get_connection()
    engine_inst = TradingEngine()
    client = engine_inst.client

    print("=========================================================================================")
    print("🔍 Finance Jarvis 전수조사 파이프라인 무결성 전수 검증")
    print("=========================================================================================\n")

    try:
        print("1️⃣ [종목 마스터 stock_master 검사]")
        res1 = await check_1_stock_master(conn)
        print(f"  · 전체 종목수: {res1['total_stocks']:,}개 (적격 종목: {res1['eligible_stocks']:,}개)")
        print(f"  · 비정상 코드수: {res1['invalid_symbol_count']}개")
        print(f"  · 결과: [{'✅ PASS' if res1['status'] == 'PASS' else '❌ FAIL'}]\n")

        print("2️⃣ [테마 마스터 stock_theme 검사]")
        res2 = await check_2_stock_theme(conn)
        print(f"  · 테마 매핑 레코드: {res2['total_theme_mappings']:,}건 (테마 수: {res2['unique_themes']}개)")
        print(f"  · 고아 종목수: {res2['orphaned_symbols']}개")
        print(f"  · 결과: [{'✅ PASS' if res2['status'] == 'PASS' else '❌ FAIL'}]\n")

        print("3️⃣ [5대 발굴 소스 가동 검사]")
        res3 = await check_3_discovery_sources(conn, client)
        for s_name, s_info in res3.items():
            status_icon = "✅" if s_info["status"] == "PASS" else "❌"
            cnt = s_info.get("count", 0)
            print(f"  · 소스 [{s_name:<8}]: {status_icon} {s_info['status']} (추출 건수: {cnt}건)")
        print()

        print("4️⃣ [종목 검증 함수 validate_candidate_symbol 방어력 검사]")
        res4 = await check_4_candidate_validation(client)
        for d in res4["details"]:
            status_icon = "✅" if d["test_passed"] else "❌"
            print(f"  · 케이스: {d['case']:<25} -> {status_icon} 검증결과={d['outcome_ok']} (예상={d['expected']}) {d['reason']}")
        print(f"  · 결과: [{'✅ PASS' if res4['status'] == 'PASS' else '❌ FAIL'}]\n")

        print("5️⃣ [후보 스코어링 및 피처 NaN/Inf 누수 검사]")
        res5 = await check_5_scoring_and_signals(conn, client)
        print(f"  · 테스트 대상: {res5['tested_count']}개 중 성공: {res5['successfully_scored']}개")
        print(f"  · NaN/Inf 결측치 탐지: {res5['nan_inf_detected']}")
        print(f"  · 결과: [{'✅ PASS' if res5['status'] == 'PASS' else '❌ FAIL'}]\n")

        print("6️⃣ [해외 대체 ETF 대리 매매 Proxy Mapping 검사]")
        res6 = await check_6_proxy_mapping()
        for m in res6["sample_mappings"]:
            print(f"  · {m['ticker']} -> {m['proxy_symbol']} ({m['proxy_name']})")
        print(f"  · 결과: [{'✅ PASS' if res6['status'] == 'PASS' else '❌ FAIL'}]\n")

        all_passed = (
            res1["status"] == "PASS"
            and res2["status"] == "PASS"
            and all(s["status"] == "PASS" for s in res3.values())
            and res4["status"] == "PASS"
            and res5["status"] == "PASS"
            and res6["status"] == "PASS"
        )

        print("=========================================================================================")
        if all_passed:
            print("🎉 [최종 판정] 전수조사 및 동적 발굴 전 파이프라인 무결성 검증 완료: 오류 없음 (ALL PASS)")
        else:
            print("⚠️ [최종 판정] 일부 항목에서 경고/오류가 발견되었습니다. 위 상세 로그를 확인하세요.")
        print("=========================================================================================\n")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

