# core/portfolio_agent.py
"""AI 포트폴리오 에이전트 오케스트레이터.

현재 승인된 포트폴리오(active_target_weights)의 맥락과 종목 발굴 후보 숏리스트를 모아
LLM에게 재비중/편입/제외를 제안받고, 안전 범위를 통과하면 PROPOSED target_weights
행으로만 저장한다. 사람이 /portfolio/rebalance-events/{id}/decide로 승인해야만 실제로
반영된다 (approval_gated 고정 — 스케줄러가 자동으로 이 함수를 호출해도 마지막 승인은
항상 사람 몫이다. full-auto는 이후 단계).
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite
import pandas as pd

from core import db, discovery, indicators, kis_domestic, quant_metrics
from core.ai_rebalance_guard import validate_proposal
from core.config import settings
from core.kis_client import AsyncKISClient
from core.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
CHART_LOOKBACK_DAYS = 90


async def _build_current_positions(
    client: Optional[AsyncKISClient],
    active_weights: Dict[str, float],
    positions: Dict[str, Dict[str, Any]],
    benchmark_returns: Optional[pd.Series],
) -> List[Dict[str, Any]]:
    today = datetime.now(tz=KST)
    start_date = (today - timedelta(days=CHART_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    result = []
    for symbol, weight in active_weights.items():
        pos = positions.get(symbol, {})
        avg_price = pos.get("avg_price", 0.0)
        entry: Dict[str, Any] = {
            "symbol": symbol,
            "weight": weight,
            "qty": pos.get("qty", 0.0),
            "avg_price": avg_price,
        }
        if client is not None:
            try:
                chart_rows = await kis_domestic.get_daily_chart(client, symbol, start_date, end_date)
                df = indicators.chart_rows_to_dataframe(chart_rows)
                entry.update(quant_metrics.compute_price_based_metrics(df, benchmark_returns))
                entry.update(indicators.compute_technical_detail(df))
                if not df.empty and avg_price > 0:
                    current_price = float(df["close"].iloc[-1])
                    entry.update(
                        quant_metrics.compute_position_return(avg_price, current_price, pos.get("entry_opened_at"))
                    )
            except Exception:
                # 해외종목(시세 API가 다름) 등에서 실패해도 이 종목 하나만 지표 없이 넘어간다.
                logger.exception(f"'{symbol}' 성과/리스크 지표 계산 실패 - 비중/평단만으로 계속")
        result.append(entry)
    return result


async def _validate_adds(
    client: AsyncKISClient, adds: List[Dict[str, Any]], candidate_pool: List[Dict[str, Any]]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """LLM이 제안한 adds를 다시 한 번 KIS로 검증한다 (candidate_pool에 있었어도 스크리닝과
    제안 사이에 VI가 새로 발동했을 수 있어 방어적으로 재확인). 통과분/반려분을 나눠 반환."""
    candidate_by_symbol = {c["symbol"]: c for c in candidate_pool}
    valid, rejected = [], []
    for add in adds:
        symbol = add.get("symbol")
        if symbol not in candidate_by_symbol:
            rejected.append({**add, "reject_reason": "candidate_pool 밖의 종목 - 채택 불가"})
            continue
        outcome = await discovery.validate_candidate_symbol(client, symbol, claimed_name=add.get("name"))
        if not outcome.ok:
            rejected.append({**add, "reject_reason": outcome.reason})
            continue
        valid.append(add)
    return valid, rejected


async def propose_rebalance(
    conn: aiosqlite.Connection,
    client: Optional[AsyncKISClient] = None,
    trigger_type: str = "MANUAL",
    trigger_detail: str = "",
) -> Dict[str, Any]:
    active_weights = await db.get_active_target_weights(conn)
    positions = await db.get_positions(conn)
    symbols = list(active_weights.keys())

    prior_snapshot = json.dumps({"weights": active_weights, "symbols": symbols}, ensure_ascii=False)
    event_id = await db.create_rebalance_event(
        conn,
        trigger_type=trigger_type,
        trigger_detail=trigger_detail,
        autonomy_mode="approval_gated",
        prior_snapshot_json=prior_snapshot,
    )

    if not symbols:
        await db.finish_rebalance_event(conn, event_id, status="SKIPPED", error="관찰 중인 종목 없음")
        return {"event_id": event_id, "status": "SKIPPED", "reason": "관찰 중인 종목 없음"}

    # 벤치마크(KODEX 200) 수익률 시계열은 베타 계산에 쓰이며, 보유종목/후보종목 전체에서
    # 공유해 재사용한다 (종목마다 다시 조회하면 그만큼 KIS 호출이 늘어남).
    benchmark_returns = await quant_metrics.fetch_benchmark_return_series(client) if client is not None else None

    current_positions = await _build_current_positions(client, active_weights, positions, benchmark_returns)
    # 뉴스감성 등 LLM 기반 시그널은 아직 연결하지 않았다 - 매 제안마다 보유종목 전부에
    # 뉴스 LLM 호출을 추가하면 비용이 커지고, 성과/리스크 지표(ROI/MDD/샤프 등)만으로도
    # Phase 5.0 대비 제안 품질이 이미 크게 개선된다. 필요성이 확인되면 별도로 연결한다.
    current_signals: Dict[str, Any] = {}

    candidate_pool: List[Dict[str, Any]] = []
    if client is not None:
        try:
            candidate_pool = await discovery.screen_candidates(
                conn, client, exclude_symbols=symbols, benchmark_returns=benchmark_returns,
            )
        except Exception:
            logger.exception("후보종목 스크리닝 실패 - 발굴 없이 재비중만 제안")

    provider = get_llm_provider()
    result = await provider.propose_portfolio_changes(
        current_positions=current_positions,
        current_signals=current_signals,
        candidate_pool=candidate_pool,
        macro_context=None,
        max_symbols=settings.AI_REBALANCE_MAX_PORTFOLIO_SYMBOLS,
    )

    if result.get("degraded"):
        context_json = json.dumps(
            {"input": {"current_positions": current_positions, "candidate_pool": candidate_pool}, "output": result},
            ensure_ascii=False, default=str,
        )
        await db.finish_rebalance_event(
            conn, event_id, status="FAILED", error=result.get("error_kind", "llm_error"),
            context_json=context_json,
        )
        return {"event_id": event_id, "status": "FAILED", "reason": result.get("rationale", "")}

    valid_adds, rejected_adds = ([], result.get("adds", []))
    if client is not None and result.get("adds"):
        valid_adds, rejected_adds = await _validate_adds(client, result["adds"], candidate_pool)
    valid_add_symbols = [a["symbol"] for a in valid_adds]

    # removes는 candidate_pool 검증 대상이 아니라 "이미 보유 중인" 종목이어야 의미가 있다.
    valid_removes = [r for r in result.get("removes", []) if r.get("symbol") in active_weights]
    remove_symbols = [r["symbol"] for r in valid_removes]

    allowed_symbols = set(active_weights) | set(valid_add_symbols)
    proposed_weights = {s: w for s, w in result.get("weights", {}).items() if s in allowed_symbols}
    for symbol in remove_symbols:
        proposed_weights[symbol] = 0.0  # 제외 = 목표비중 0 (기존 ceiling 로직으로 점진 청산)

    context_json = json.dumps(
        {
            "input": {"current_positions": current_positions, "candidate_pool": candidate_pool},
            "output": result,
            "rejected_adds": rejected_adds,
        },
        ensure_ascii=False, default=str,
    )

    validation = validate_proposal(
        proposed_weights,
        active_weights,
        adds=valid_add_symbols,
        removes=remove_symbols,
        max_turnover_pct=settings.AI_REBALANCE_MAX_TURNOVER_PCT,
        max_weight_delta_pct=settings.AI_REBALANCE_MAX_WEIGHT_DELTA_PCT,
        max_symbols_added=settings.AI_REBALANCE_MAX_SYMBOLS_ADDED,
        max_symbols_removed=settings.AI_REBALANCE_MAX_SYMBOLS_REMOVED,
        min_symbol_weight_pct=settings.AI_REBALANCE_MIN_SYMBOL_WEIGHT_PCT,
        max_symbol_weight_pct=settings.MAX_POSITION_PCT,
        max_portfolio_symbols=settings.AI_REBALANCE_MAX_PORTFOLIO_SYMBOLS,
    )
    if not validation.ok:
        await db.finish_rebalance_event(
            conn, event_id, status="BLOCKED", error=validation.reason, context_json=context_json,
        )
        return {"event_id": event_id, "status": "BLOCKED", "reason": validation.reason}

    proposal_ids = []
    for symbol, weight in proposed_weights.items():
        proposal_id = await db.propose_target_weight(
            conn, symbol, weight, proposed_by="llm",
            rationale=result.get("rationale", ""), rebalance_event_id=event_id,
        )
        proposal_ids.append(proposal_id)

    proposed_snapshot = json.dumps(
        {"weights": proposed_weights, "symbols": list(set(symbols) | set(valid_add_symbols))},
        ensure_ascii=False,
    )
    await db.finish_rebalance_event(
        conn, event_id, status="PROPOSED", proposed_snapshot_json=proposed_snapshot,
        rationale=result.get("rationale", ""), context_json=context_json,
        symbols_added_json=json.dumps(valid_adds, ensure_ascii=False) if valid_adds else None,
        symbols_removed_json=json.dumps(valid_removes, ensure_ascii=False) if valid_removes else None,
    )
    return {
        "event_id": event_id,
        "status": "PROPOSED",
        "proposal_ids": proposal_ids,
        "adds": valid_add_symbols,
        "removes": remove_symbols,
        "rationale": result.get("rationale", ""),
    }
