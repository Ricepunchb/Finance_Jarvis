# core/us_market/rebalance.py
"""미국 주식 전용 계좌(순수 USD) 자동 리밸런싱 엔진.

핵심 원칙:
1. 현금 유입형 리밸런싱 (Cash-Inflow Rebalancing):
   - 사용자가 모바일 앱에서 달러를 환전/입금했을 때, 기존 보유 주식을 1주도 팔지 않고
     (해외주식 양도소득세 22% 원천 방지) 부족한 종목부터 채워 넣는 세금 절감형 매수 리밸런싱.
2. 밴드 드리프트 리밸런싱 (Band Drift Rebalancing):
   - 특정 종목이 급등하여 목표 비중 대비 +5.0%p 이상 초과(Overweight) 시에만
     초과분만큼 분할 매도하여 이익을 확정하고 소외주에 재투자.
3. 순수 USD 회계 및 원화 자동환전 원천 방지:
   - intg_marg_iqry_yn='N' 강제 및 실시간 available_usd 가드로 순수 달러만 사용.
4. 미국 정규장(22:30~05:00 KST) 정수 1주 단위 분할 집행 (TWAP).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from core import db
from core.config import settings
from core.us_market.client import get_us_quote
from core.us_market.surprise import strip_all_emojis

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 기본 미국장 포트폴리오 목표 비중 (총합 1.0 = 100%)
DEFAULT_US_TARGET_WEIGHTS: Dict[str, float] = {
    "VOO": 0.20,   # S&P500 대표 지수 (Core)
    "SCHD": 0.20,  # 미국 배당 다우존스 (Core Dividend)
    "NVDA": 0.20,  # AI 하드웨어 대장주 (Growth)
    "PLTR": 0.15,  # 엔터프라이즈 AI 소프트웨어 (Growth)
    "SPCX": 0.08,  # S&P500 고배당 인컴 ETF (Income)
    "TSLW": 0.07,  # 테슬라 연계 커버드콜 인컴 ETF (Income)
    "SGOV": 0.10,  # 미국 초단기 국채 (Cash Yield Buffer)
}

# 기본 허용 오차 밴드 (±5.0%p)
DEFAULT_REBALANCE_BAND_PCT = 0.05

# 종목별 기본 거래소 매핑 (seed 파일 동적 로드 + 기본값)
def _load_symbol_exchange_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {
        "NVDA": "NASD",
        "PLTR": "NASD",
        "SPCX": "NASD",
        "VOO": "AMEX",
        "SCHD": "AMEX",
        "TSLW": "AMEX",
        "SGOV": "AMEX",
        "GLD": "AMEX",
        "SPY": "AMEX",
        "QQQ": "NASD",
    }
    seed_path = Path("data/us_candidate_universe_seed.json")
    if seed_path.exists():
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    sym = item.get("symbol")
                    ex = item.get("exchange")
                    if sym and ex:
                        mapping[sym.strip().upper()] = ex.strip().upper()
        except Exception as e:
            logger.warning("Failed to load US candidate seed exchange map: %s", e)
    return mapping


SYMBOL_EXCHANGE_MAP: Dict[str, str] = _load_symbol_exchange_map()


class USPositionStatus(BaseModel):
    symbol: str
    name: str = ""
    exchange: str = "NASD"
    qty: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    eval_value_usd: float = 0.0
    pnl_pct: float = 0.0
    current_weight: float = 0.0
    target_weight: float = 0.0
    drift_pct: float = 0.0  # current_weight - target_weight
    status: str = "BALANCED"  # EXCESS | DEFICIT | BALANCED | EXCESS_LOCKED | EXCESS_LOSS_LOCKED | UNMANAGED


class USRebalanceItem(BaseModel):
    symbol: str
    exchange: str = "NASD"
    action: str  # BUY | SELL
    qty: int  # 정수 1주 단위
    estimated_price: float
    estimated_amount_usd: float
    reason: str
    pre_weight: float
    post_weight: float


class USRebalancePlan(BaseModel):
    trigger_type: str  # CASH_INFLOW | BAND_DRIFT | SCHEDULED | MANUAL | BALANCED
    status: str = "PROPOSED"  # PROPOSED | EXECUTED | SKIPPED | REJECTED
    total_equity_usd: float
    available_usd: float
    inflow_detected_usd: float = 0.0
    items: List[USRebalanceItem] = Field(default_factory=list)
    total_buy_usd: float = 0.0
    total_sell_usd: float = 0.0
    projected_residual_usd: float = 0.0
    positions: List[USPositionStatus] = Field(default_factory=list)
    rationale: str = ""
    created_at: float = Field(default_factory=time.time)


async def get_us_target_weights(conn) -> Dict[str, float]:
    """DB에 저장된 미국장 종목들의 목표 비중을 조회한다. 없으면 기본값을 세팅(메리츠 해외 전용 계좌일 때만)."""
    # 국내 전용 계좌(DOMESTIC_ONLY=True 또는 BROKER != 'meritz')에서는 미국장 비중 자동 주입 및 조회를 원천 차단
    if getattr(settings, "DOMESTIC_ONLY", False) or getattr(settings, "BROKER", "kis").lower() != "meritz":
        return {}

    active = await db.get_active_target_weights(conn)
    # overseas 종목만 필터
    overseas_symbols = set(DEFAULT_US_TARGET_WEIGHTS.keys())
    try:
        cur = await conn.execute("SELECT symbol FROM portfolio_symbols WHERE market = 'overseas' AND enabled = 1")
        rows = await cur.fetchall()
        if rows:
            overseas_symbols = {r["symbol"].upper() for r in rows}
    except Exception:
        pass

    us_weights = {k.upper(): v for k, v in active.items() if k.upper() in overseas_symbols and v > 0}
    if not us_weights:
        # 메리츠 해외 전용 계좌일 때만 초기 기본값 저장 및 반환
        for sym, w in DEFAULT_US_TARGET_WEIGHTS.items():
            prop_id = await db.propose_target_weight(
                conn, sym, w, proposed_by="system", rationale="미국장 기본 자산배분 목표 비중"
            )
            await db.decide_target_weight(conn, prop_id, approve=True)
        return dict(DEFAULT_US_TARGET_WEIGHTS)
    return us_weights


async def set_us_target_weights(conn, weights: Dict[str, float]) -> Dict[str, float]:
    """미국장 포트폴리오 목표 비중을 업데이트하고 정규화한다 (신규 편입 종목은 portfolio_symbols/us_watchlist에 자동 등록)."""
    if getattr(settings, "DOMESTIC_ONLY", False) or getattr(settings, "BROKER", "kis").lower() != "meritz":
        raise ValueError("국내 전용 계좌에서는 미국장 목표 비중을 설정할 수 없습니다.")

    clean = {k.strip().upper(): max(0.0, float(v)) for k, v in weights.items() if k.strip()}
    total = sum(clean.values())
    if total > 0:
        clean = {k: round(v / total, 4) for k, v in clean.items()}

    existing_wl = {it["symbol"].upper(): it for it in await db.list_us_watchlist(conn)}
    for sym, w in clean.items():
        if w > 0:
            exch = SYMBOL_EXCHANGE_MAP.get(sym, "NASD")
            await db.add_portfolio_symbol(conn, sym, market="overseas", exchange=exch)
            if sym not in existing_wl:
                await db.upsert_us_watchlist_item(
                    conn=conn,
                    symbol=sym,
                    name=sym,
                    sector="AI 발굴 편입",
                    target_weight=w,
                    alert_enabled=True,
                )
            else:
                it = existing_wl[sym]
                await db.upsert_us_watchlist_item(
                    conn=conn,
                    symbol=sym,
                    name=it.get("name") or sym,
                    sector=it.get("sector"),
                    avg_entry_price=it.get("avg_entry_price"),
                    held_qty=it.get("held_qty"),
                    target_weight=w,
                    alert_enabled=bool(it.get("alert_enabled", True)),
                    notes=it.get("notes"),
                )

        prop_id = await db.propose_target_weight(
            conn, sym, w, proposed_by="user", rationale="미국장 포트폴리오 목표 비중 설정"
        )
        await db.decide_target_weight(conn, prop_id, approve=True)
    return clean


async def analyze_us_portfolio_drift(
    conn,
    client: Optional[Any] = None,
    band_pct: float = DEFAULT_REBALANCE_BAND_PCT,
    target_weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """미국 계좌의 현재 포지션, 가용 달러, 실시간 시세를 분석하여 비중 드리프트를 진단한다."""
    if getattr(settings, "DOMESTIC_ONLY", False) or getattr(settings, "BROKER", "kis").lower() != "meritz":
        return {
            "status": "SKIPPED",
            "message": "국내 전용 계좌에서는 미국장 리밸런싱을 지원하지 않습니다.",
            "total_equity_usd": 0.0,
            "total_stock_eval_usd": 0.0,
            "available_usd": 0.0,
            "inflow_detected_usd": 0.0,
            "positions": [],
            "excess_positions": [],
            "deficit_positions": [],
            "balanced_positions": [],
            "band_pct": band_pct,
        }

    targets = target_weights or await get_us_target_weights(conn)

    # 1. 메리츠 API 연동 잔고 조회 시도
    holdings_dict: Dict[str, Dict[str, Any]] = {}
    available_usd = 0.0

    from core.meritz_overseas import get_present_balance_krw
    is_meritz = getattr(settings, "BROKER", "kis").lower() == "meritz"

    if is_meritz and client is not None:
        try:
            bal = await get_present_balance_krw(client)
            available_usd = float(bal.get("totals", {}).get("available_usd") or 0.0)
            for h in bal.get("holdings", []):
                sym = str(h.get("symbol") or h.get("ovrs_pdno") or "").upper().strip()
                if sym:
                    holdings_dict[sym] = h
        except Exception as e:
            logger.warning(f"메리츠 잔고 실시간 조회 실패 - DB 캐시 사용: {e}")

    # 잔고 조회가 실패하거나 클라이언트가 없으면 DB positions 및 state 사용
    if not holdings_dict:
        db_positions = await db.get_positions(conn)
        for sym_key, p in db_positions.items():
            sym = str(sym_key).upper()
            mkt = p.get("market") if isinstance(p, dict) else getattr(p, "market", "overseas")
            qty = float(p.get("qty", 0.0) if isinstance(p, dict) else getattr(p, "qty", 0.0))
            avg_p = float(p.get("avg_price") or p.get("avg_entry_price") or 0.0)
            if sym in targets or mkt == "overseas" or qty > 0:
                holdings_dict[sym] = {
                    "symbol": sym,
                    "qty": qty,
                    "avg_entry_price": avg_p,
                    "exchange": SYMBOL_EXCHANGE_MAP.get(sym, "NASD"),
                }

    if available_usd <= 0:
        saved_cash = await db.get_state(conn, "us_last_known_available_usd")
        if saved_cash:
            try:
                available_usd = float(saved_cash)
            except Exception:
                pass

    # 목표 비중에 포함된 종목 + 계좌에 실보유 중인 종목을 모두 합산 (누락 방지)
    held_symbols = {
        sym for sym, h in holdings_dict.items()
        if float(h.get("cblc_qty13") or h.get("qty") or 0.0) > 0
    }
    symbols = sorted(set(targets.keys()) | held_symbols)

    # 2. 실시간 시세 비동기 수집 (yfinance 또는 client)
    quote_tasks = [get_us_quote(sym) for sym in symbols]
    quotes_list = await asyncio.gather(*quote_tasks, return_exceptions=True)
    quotes: Dict[str, float] = {}
    names: Dict[str, str] = {}

    for sym, q in zip(symbols, quotes_list):
        if isinstance(q, dict) and q.get("price"):
            quotes[sym] = float(q["price"])
            if q.get("name"):
                names[sym] = q["name"]
        else:
            # 기본값 fallback
            quotes[sym] = float(holdings_dict.get(sym, {}).get("avg_entry_price") or 100.0)

    # 3. 각 종목별 평가금액 및 총 자산(Total Equity USD) 계산
    pos_evals: Dict[str, float] = {}
    pos_qtys: Dict[str, float] = {}

    for sym in symbols:
        h = holdings_dict.get(sym, {})
        qty = float(h.get("cblc_qty13") or h.get("qty") or 0.0)
        p = quotes.get(sym, 0.0)
        eval_usd = qty * p
        pos_qtys[sym] = qty
        pos_evals[sym] = eval_usd

    total_stock_eval_usd = sum(pos_evals.values())
    total_equity_usd = total_stock_eval_usd + available_usd
    if total_equity_usd <= 0:
        total_equity_usd = 1000.0  # 방어적 기본값

    # 4. 이전 가용 달러 대비 입금(Inflow) 감지
    last_known_cash_str = await db.get_state(conn, "us_last_known_available_usd")
    last_known_cash = float(last_known_cash_str) if last_known_cash_str else available_usd
    inflow_detected_usd = max(0.0, available_usd - last_known_cash)

    # 5. 종목별 비중 및 드리프트 계산
    statuses: List[USPositionStatus] = []
    excess_list: List[USPositionStatus] = []
    deficit_list: List[USPositionStatus] = []

    for sym in symbols:
        cur_val = pos_evals.get(sym, 0.0)
        cur_w = (cur_val / total_equity_usd) if total_equity_usd > 0 else 0.0
        tgt_w = targets.get(sym, 0.0)
        drift = cur_w - tgt_w
        cur_qty = pos_qtys.get(sym, 0.0)
        cur_price = quotes.get(sym, 0.0)
        excess_val = (cur_w - (tgt_w + band_pct)) * total_equity_usd

        h_info = holdings_dict.get(sym, {})
        avg_entry = float(h_info.get("avg_entry_price") or h_info.get("avg_price") or 0.0)
        pnl = ((cur_price - avg_entry) / avg_entry * 100) if avg_entry > 0 and cur_price > 0 else 0.0

        if tgt_w == 0.0 and cur_qty > 0:
            stat_label = "UNMANAGED"
        elif drift > band_pct:
            # 1. 고단가 1주 보호: 초과 금액이 1주 단가보다 작거나, 보유 수량이 1주 이하인 경우 슬림화 매도 불가
            if cur_qty <= 1.0 or (cur_price > 0 and excess_val < cur_price):
                stat_label = "EXCESS_LOCKED"
            # 2. 손실 보호 가드: 평단가 대비 마이너스 손실(-1.0% 초과 손실)인 종목은 바닥 강제 손실 확정 방지를 위해 매도 제외
            elif avg_entry > 0 and cur_price < avg_entry * 0.99:
                stat_label = "EXCESS_LOSS_LOCKED"
            else:
                stat_label = "EXCESS"
        elif drift < -band_pct:
            stat_label = "DEFICIT"
        else:
            stat_label = "BALANCED"

        status_obj = USPositionStatus(
            symbol=sym,
            name=names.get(sym, sym),
            exchange=SYMBOL_EXCHANGE_MAP.get(sym, "NASD"),
            qty=pos_qtys.get(sym, 0.0),
            avg_price=round(avg_entry, 2),
            current_price=round(quotes.get(sym, 0.0), 2),
            eval_value_usd=round(cur_val, 2),
            pnl_pct=round(pnl, 2),
            current_weight=round(cur_w, 4),
            target_weight=round(tgt_w, 4),
            drift_pct=round(drift, 4),
            status=stat_label,
        )
        statuses.append(status_obj)
        if stat_label == "EXCESS":
            excess_list.append(status_obj)
        elif stat_label == "DEFICIT":
            deficit_list.append(status_obj)

    return {
        "total_equity_usd": round(total_equity_usd, 2),
        "total_stock_eval_usd": round(total_stock_eval_usd, 2),
        "available_usd": round(available_usd, 2),
        "inflow_detected_usd": round(inflow_detected_usd, 2),
        "band_pct": band_pct,
        "positions": statuses,
        "excess_positions": excess_list,
        "deficit_positions": deficit_list,
        "quotes": quotes,
    }


def build_us_rebalance_plan(
    drift_data: Dict[str, Any],
    force_inflow_rebalance: bool = False,
) -> USRebalancePlan:
    """드리프트 진단 결과를 바탕으로 세금 절감형 매매 계획을 수립한다."""
    total_equity = drift_data["total_equity_usd"]
    available_usd = drift_data["available_usd"]
    inflow_usd = drift_data["inflow_detected_usd"]
    positions: List[USPositionStatus] = drift_data["positions"]
    excess_positions: List[USPositionStatus] = drift_data["excess_positions"]
    deficit_positions: List[USPositionStatus] = drift_data["deficit_positions"]
    band_pct = drift_data["band_pct"]

    # 트리거 판별
    items: List[USRebalanceItem] = []
    total_sell_usd = 0.0
    total_buy_usd = 0.0

    # 1. 밴드 상한 초과 종목이 있는 경우 (BAND_DRIFT) -> 과비중 종목만 정밀 슬림화 매도
    if excess_positions and not force_inflow_rebalance:
        trigger = "BAND_DRIFT"
        rationale = f"일부 종목이 목표비중 대비 +{int(band_pct*100)}%p를 초과하여 초과분 매도 및 저비중 재배치"

        for p in excess_positions:
            excess_pct = p.current_weight - (p.target_weight + band_pct)
            if excess_pct > 0 and p.current_price > 0:
                excess_val = excess_pct * total_equity
                sell_qty = int(math.floor(excess_val / p.current_price))
                if sell_qty > 0 and sell_qty <= p.qty:
                    amt = round(sell_qty * p.current_price, 2)
                    total_sell_usd += amt
                    post_w = round((p.eval_value_usd - amt) / total_equity, 4)
                    action_desc = "이익실현" if p.pnl_pct >= 0 else "비중 축소"
                    pnl_str = f"수익률 {p.pnl_pct:+.1f}%, " if p.avg_price > 0 else ""
                    items.append(
                        USRebalanceItem(
                            symbol=p.symbol,
                            exchange=p.exchange,
                            action="SELL",
                            qty=sell_qty,
                            estimated_price=p.current_price,
                            estimated_amount_usd=amt,
                            reason=f"밴드 상한 초과분 {action_desc} ({pnl_str}{p.current_weight*100:.1f}% -> {post_w*100:.1f}%)",
                            pre_weight=p.current_weight,
                            post_weight=post_w,
                        )
                    )

    # 2. 신규 가용 달러가 충분하고 저비중 종목이 있는 경우 (CASH_INFLOW) -> 세금 없는 매수 리밸런싱
    elif available_usd >= 30.0 and (inflow_usd >= 30.0 or deficit_positions or force_inflow_rebalance):
        trigger = "CASH_INFLOW"
        rationale = "가용 달러(USD) 유입에 따라 기존 주식 매도 없이(양도세 0원) 부족 종목 분할 매수"

    elif deficit_positions:
        trigger = "DEFICIT_REBALANCE"
        rationale = f"목표 비중 대비 -{int(band_pct*100)}%p 이상 미달 종목 보충"
    else:
        trigger = "BALANCED"
        rationale = f"모든 종목이 목표 비중 허용오차(±{int(band_pct*100)}%p) 이내로 균형 유지 중"
        return USRebalancePlan(
            trigger_type="BALANCED",
            status="SKIPPED",
            total_equity_usd=total_equity,
            available_usd=available_usd,
            inflow_detected_usd=inflow_usd,
            items=[],
            positions=positions,
            rationale=rationale,
        )

    # 매수 예산 계산 = 현재 가용 달러 + 매도 예상 회수 달러
    buy_budget = available_usd + total_sell_usd

    # 저비중 종목들에게 결손액(gap) 비율로 매수 예산 배분
    candidates = deficit_positions if deficit_positions else [p for p in positions if p.drift_pct < 0]
    total_deficit_usd = sum(abs(p.drift_pct) * total_equity for p in candidates)

    if buy_budget >= 15.0 and total_deficit_usd > 0:
        # 결손액이 큰 종목 순서로 정렬
        sorted_candidates = sorted(candidates, key=lambda p: abs(p.drift_pct), reverse=True)
        remaining_budget = buy_budget

        for p in sorted_candidates:
            if remaining_budget < 15.0 or p.current_price <= 0:
                continue

            gap_usd = abs(p.drift_pct) * total_equity
            # 비례 배분 금액
            weight_ratio = gap_usd / total_deficit_usd
            alloc_usd = min(remaining_budget, buy_budget * weight_ratio)

            # 정수 1주 단위 계산
            buy_qty = int(math.floor(alloc_usd / p.current_price))
            if buy_qty <= 0 and remaining_budget >= p.current_price:
                # 최소 1주는 살 수 있는 경우 1주 매수
                buy_qty = 1

            if buy_qty > 0:
                cost = round(buy_qty * p.current_price, 2)
                if cost <= remaining_budget:
                    remaining_budget -= cost
                    total_buy_usd += cost
                    post_w = round((p.eval_value_usd + cost) / total_equity, 4)
                    items.append(
                        USRebalanceItem(
                            symbol=p.symbol,
                            exchange=p.exchange,
                            action="BUY",
                            qty=buy_qty,
                            estimated_price=p.current_price,
                            estimated_amount_usd=cost,
                            reason=f"저비중 채움 매수 ({p.current_weight*100:.1f}% -> {post_w*100:.1f}%)",
                            pre_weight=p.current_weight,
                            post_weight=post_w,
                        )
                    )

    projected_residual = round(available_usd + total_sell_usd - total_buy_usd, 2)

    return USRebalancePlan(
        trigger_type=trigger,
        status="PROPOSED" if items else "SKIPPED",
        total_equity_usd=total_equity,
        available_usd=available_usd,
        inflow_detected_usd=inflow_usd,
        items=items,
        total_buy_usd=round(total_buy_usd, 2),
        total_sell_usd=round(total_sell_usd, 2),
        projected_residual_usd=projected_residual,
        positions=positions,
        rationale=rationale,
    )


def format_us_rebalance_telegram_message(plan: USRebalancePlan) -> str:
    """이모지를 완전히 배제하고 가독성을 극대화한 미국장 리밸런싱 계획 텔레그램 메시지."""
    now_kst = datetime.fromtimestamp(plan.created_at, tz=KST).strftime("%m-%d %H:%M KST")

    if plan.trigger_type == "CASH_INFLOW":
        trigger_label = "달러 신규 입금 감지 (세금 없는 매수 리밸런싱)"
    elif plan.trigger_type == "BAND_DRIFT":
        trigger_label = "비중 허용오차(±5%p) 이탈 감지"
    elif plan.trigger_type == "DEFICIT_REBALANCE":
        trigger_label = "목표 비중 미달 종목 보충"
    else:
        trigger_label = "포트폴리오 정기 재조정"

    sells = [it for it in plan.items if it.action == "SELL"]
    buys = [it for it in plan.items if it.action == "BUY"]

    lines = [
        "[미국장 포트폴리오 리밸런싱 계획]",
        f"- 감지 유형: {trigger_label}",
        f"- 총 평가자산: ${plan.total_equity_usd:,.2f}",
        f"- 가용 달러(USD): ${plan.available_usd:,.2f}",
    ]

    if sells:
        lines.append("\n[매도 예정 (과비중 슬림화)]")
        for idx, s in enumerate(sells, 1):
            lines.append(
                f"{idx}. {s.symbol}: {s.qty}주 (약 ${s.estimated_amount_usd:,.2f}, {s.pre_weight*100:.1f}% -> {s.post_weight*100:.1f}%)"
            )

    if buys:
        lines.append("\n[매수 예정 (저비중 채움)]")
        for idx, b in enumerate(buys, 1):
            lines.append(
                f"{idx}. {b.symbol}: {b.qty}주 (약 ${b.estimated_amount_usd:,.2f}, {b.pre_weight*100:.1f}% -> {b.post_weight*100:.1f}%)"
            )

    lines.extend([
        "",
        f"- 총 매도 회수액: ${plan.total_sell_usd:,.2f}",
        f"- 총 매수 투입액: ${plan.total_buy_usd:,.2f}",
        f"- 예상 잔여 달러: ${plan.projected_residual_usd:,.2f}",
        f"- 비고: {plan.rationale}",
        f"- 기준 시각: {now_kst}",
    ])

    return strip_all_emojis("\n".join(lines))


async def execute_us_rebalance_plan(
    conn,
    plan: USRebalancePlan,
    client: Optional[Any] = None,
    shadow_mode: Optional[bool] = None,
) -> Dict[str, Any]:
    """리밸런싱 계획을 정규장 시간에 매도 우선 -> 매수 순서로 안전하게 집행한다."""
    if not plan.items:
        return {"status": "SKIPPED", "executed_count": 0, "message": "실행할 매매 내역 없음"}

    from core.meritz_overseas import get_buyable_cash, order
    is_shadow = shadow_mode if shadow_mode is not None else getattr(settings, "SHADOW_MODE", False)

    sells = [it for it in plan.items if it.action == "SELL"]
    buys = [it for it in plan.items if it.action == "BUY"]

    executed_orders: List[Dict[str, Any]] = []
    failed_orders: List[Dict[str, Any]] = []

    # 1. 매도 주문 우선 집행 (현금 확보)
    for s in sells:
        try:
            if client is not None and not is_shadow:
                res = await order(
                    client=client,
                    ovrs_excg_cd=s.exchange,
                    symbol=s.symbol,
                    side="sell",
                    qty=s.qty,
                    price=s.estimated_price,
                    shadow=False,
                )
                odno = res.get("ODNO") or res.get("odno") or "FILLED"
            else:
                odno = f"SIM_SELL_{int(time.time())}"

            executed_orders.append({
                "symbol": s.symbol,
                "action": "SELL",
                "qty": s.qty,
                "price": s.estimated_price,
                "amount": s.estimated_amount_usd,
                "order_no": odno,
            })
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"'{s.symbol}' 리밸런싱 매도 실패: {e}")
            failed_orders.append({"symbol": s.symbol, "action": "SELL", "error": str(e)})

    # 2. 매수 주문 집행 (매도 체결 후 보유 달러 가드 적용)
    for b in buys:
        try:
            avail_usd = None
            if client is not None and not is_shadow:
                # 실시간 달러 예수금 재확인 (원화 자동환전 방지)
                chk = await get_buyable_cash(client, b.exchange, b.symbol, b.estimated_price)
                avail_usd = float(chk.get("ovrs_ord_psbl_amt") or 0.0)
                max_q = int(chk.get("max_ord_psbl_qty") or 0)
                actual_qty = min(b.qty, max_q)
                if actual_qty <= 0:
                    logger.warning(f"'{b.symbol}' 가용 달러 부족으로 매수 스킵")
                    continue

                res = await order(
                    client=client,
                    ovrs_excg_cd=b.exchange,
                    symbol=b.symbol,
                    side="buy",
                    qty=actual_qty,
                    price=b.estimated_price,
                    shadow=False,
                    available_usd=avail_usd,
                )
                odno = res.get("ODNO") or res.get("odno") or "FILLED"
            else:
                actual_qty = b.qty
                odno = f"SIM_BUY_{int(time.time())}"

            executed_orders.append({
                "symbol": b.symbol,
                "action": "BUY",
                "qty": actual_qty,
                "price": b.estimated_price,
                "amount": round(actual_qty * b.estimated_price, 2),
                "order_no": odno,
            })
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"'{b.symbol}' 리밸런싱 매수 실패: {e}")
            failed_orders.append({"symbol": b.symbol, "action": "BUY", "error": str(e)})

    # 가용 달러 상태 갱신
    await db.set_state(conn, "us_last_known_available_usd", str(plan.projected_residual_usd))
    await db.set_state(conn, "us_last_rebalance_executed_at", str(time.time()))

    return {
        "status": "SUCCESS" if executed_orders else "FAILED",
        "trigger_type": plan.trigger_type,
        "executed_count": len(executed_orders),
        "failed_count": len(failed_orders),
        "executed_orders": executed_orders,
        "failed_orders": failed_orders,
    }
