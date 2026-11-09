# core/us_market/discovery.py
"""미국장(US Market) 점수 기반 글로벌 유망 종목 발굴 & 스마트 포트폴리오 진화 엔진.

기존의 '고정 종목 관리 + 리밸런싱'을 초기값(Init)으로 삼아,
국장(국내주식) 발굴 엔진 수준 이상의 5대 멀티팩터 스코어링(0~100점)을 통해
글로벌 주도 섹터 및 유망 미국 종목을 능동적으로 발굴하고 포트폴리오 비중 진화를 제안합니다.

[5대 멀티팩터 채점 체계 (총 100점 만점)]
1. 퀀트 & 기술적 모멘텀 (25점): 추세 정배열(SMA20/60), 샤프 지수, 거래량 수급, RSI/MACD 시그널
2. 월가 애널리스트 컨센서스 & 리포트 (25점): 목표주가 괴리율(Upside %), 투자의견(Strong Buy/Buy), 리서치 커버리지
3. 글로벌 매크로 & 유망 섹터 로테이션 (20점): S&P 500 섹터 ETF 상대강도(vs SPY) 및 주도 테마 부합도
4. 펀더멘털 & 밸류에이션 (15점): PER/Forward PE 적정성, 이익 성장성, 52주 위치
5. 뉴스 속보 & AI Surprise 모멘텀 (15점): 최신 외신 뉴스 군집화 호재/악재 및 AI Surprise 점수 반영
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from core import db
from core.fundamentals.overseas_consensus import fetch_overseas_consensus, fetch_overseas_research
from core.us_market.client import get_us_consensus_and_news, get_us_daily_bars, get_us_quote
from core.us_market.signals import compute_technical_signal, extract_indicator_snapshot

logger = logging.getLogger(__name__)

US_SEED_PATH = Path("data/us_candidate_universe_seed.json")
STATE_US_DISCOVERY_CACHE = "us_discovery_scored_cache_json"
US_DISCOVERY_CACHE_TTL_SEC = 3600 * 4  # 4시간 캐시 유지
DEFAULT_US_DISCOVERY_MIN_SCORE = 67.5  # 일일 종합점수 분포 기준 상위 12% 컷오프 (보수적 편입 기준)

# S&P 500 대표 섹터 및 매크로 테마 ETF 정의
US_SECTOR_ETFS: Dict[str, str] = {
    "XLK": "AI & 테크놀로지",
    "XLI": "전력 인프라 & 산업재 & 방산",
    "XLV": "바이오 & 헬스케어",
    "XLF": "금융 & 배당가치",
    "XLC": "빅테크 플랫폼 & 커뮤니케이션",
    "XLY": "자율주행 & 소비재",
    "XLU": "유틸리티 & 원전 에너지",
    "XLP": "필수소비재 방어주",
    "GLD": "금 & 실물 안전자산",
    "SGOV": "단기국채 & 달러 현금성",
}


class USSectorLeaderItem(BaseModel):
    sector_etf: str
    sector_name: str
    current_price: float = 0.0
    return_1m_pct: float = 0.0
    return_3m_pct: float = 0.0
    rel_strength_1m: float = 0.0  # SPY 대비 1개월 초과수익률(%p)
    rel_strength_3m: float = 0.0  # SPY 대비 3개월 초과수익률(%p)
    is_uptrend: bool = False
    sector_score: float = 50.0    # 0 ~ 100점 환산
    rank: int = 0
    regime_label: str = "중립"


class USCandidateScoreItem(BaseModel):
    symbol: str
    name: str
    sector: str
    sector_etf: str
    universe_tag: str
    exchange: str = "NASD"
    current_price: float = 0.0
    change_1d_pct: float = 0.0
    return_1m_pct: float = 0.0
    return_3m_pct: float = 0.0
    sharpe_ratio: float = 0.0
    volatility_pct: float = 0.0
    rsi_14: float = 50.0
    is_uptrend: bool = False
    tech_signal_dir: str = "HOLD"

    # 월가 컨센서스 & 밸류에이션
    target_mean_price: Optional[float] = None
    target_upside_pct: Optional[float] = None
    recommendation_key: str = ""
    recommendation_mean: Optional[float] = None
    per: Optional[float] = None
    pbr: Optional[float] = None
    report_count: int = 0
    latest_report_title: str = ""

    # 뉴스 속보 & AI Surprise
    latest_news_title: str = ""
    news_surprise_score: int = 0
    news_impact_type: str = "중립"

    # 5대 팩터 세부 점수 (합계 100점 만점)
    quant_tech_pts: float = 0.0       # 만점 25점
    consensus_pts: float = 0.0        # 만점 25점
    sector_macro_pts: float = 0.0     # 만점 20점
    valuation_pts: float = 0.0        # 만점 15점
    news_surprise_pts: float = 0.0    # 만점 15점
    total_score: float = 0.0          # 0 ~ 100점

    # 포트폴리오 편입 상태 및 투자 포인트
    is_in_portfolio: bool = False
    current_target_weight: float = 0.0
    grade: str = "B"
    thesis: str = ""
    rank: int = 0


class USPortfolioEvolutionProposal(BaseModel):
    base_portfolio_count: int
    proposed_portfolio_count: int
    added_symbols: List[str] = Field(default_factory=list)
    retained_symbols: List[str] = Field(default_factory=list)
    proposed_weights: Dict[str, float] = Field(default_factory=dict)
    items: List[Dict[str, Any]] = Field(default_factory=list)
    summary_reason: str = ""


def load_us_seed_candidates() -> List[Dict[str, Any]]:
    """미국장 발굴 후보 시드 파일(`data/us_candidate_universe_seed.json`)을 로드한다."""
    if not US_SEED_PATH.exists():
        return []
    try:
        return json.loads(US_SEED_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"미국장 후보 시드 로드 실패: {e}")
        return []


def _compute_return_and_sharpe(df: pd.DataFrame) -> Dict[str, float]:
    """일봉 데이터프레임에서 1개월/3개월 수익률, 연율화 샤프지수, 변동성을 계산한다."""
    if df is None or df.empty or "close" not in df.columns or len(df) < 5:
        return {"ret_1m": 0.0, "ret_3m": 0.0, "sharpe": 0.0, "vol": 0.0, "is_uptrend": False}

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < 5:
        return {"ret_1m": 0.0, "ret_3m": 0.0, "sharpe": 0.0, "vol": 0.0, "is_uptrend": False}

    last = float(closes.iloc[-1])
    idx_1m = max(0, len(closes) - 21)
    idx_3m = max(0, len(closes) - 63)
    p_1m = float(closes.iloc[idx_1m])
    p_3m = float(closes.iloc[idx_3m])

    ret_1m = ((last - p_1m) / p_1m * 100.0) if p_1m > 0 else 0.0
    ret_3m = ((last - p_3m) / p_3m * 100.0) if p_3m > 0 else 0.0

    rets = closes.pct_change().dropna()
    if len(rets) >= 10 and rets.std() > 1e-9:
        vol = float(rets.std() * np.sqrt(252) * 100.0)
        mean_ann = float(rets.mean() * 252)
        sharpe = round((mean_ann - 0.04) / (rets.std() * np.sqrt(252)), 2)
    else:
        vol = 0.0
        sharpe = 0.0

    sma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else last
    sma60 = float(closes.tail(60).mean()) if len(closes) >= 60 else sma20
    is_uptrend = bool(last >= sma20 and sma20 >= sma60 * 0.98)

    return {
        "ret_1m": round(ret_1m, 2),
        "ret_3m": round(ret_3m, 2),
        "sharpe": sharpe,
        "vol": round(vol, 2),
        "is_uptrend": is_uptrend,
    }


async def analyze_us_sector_leadership() -> List[USSectorLeaderItem]:
    """글로벌 판 분석: S&P 500 주요 섹터 ETF들의 SPY 대비 상대강도(Relative Strength)와 추세를 평가한다."""
    all_etfs = ["SPY"] + list(US_SECTOR_ETFS.keys())
    bars_map: Dict[str, pd.DataFrame] = {}

    tasks = [get_us_daily_bars(sym, period="6mo") for sym in all_etfs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for sym, res in zip(all_etfs, results):
        if isinstance(res, pd.DataFrame) and not res.empty:
            bars_map[sym] = res

    spy_stats = _compute_return_and_sharpe(bars_map.get("SPY", pd.DataFrame()))
    spy_1m = spy_stats["ret_1m"]
    spy_3m = spy_stats["ret_3m"]

    sector_items: List[USSectorLeaderItem] = []
    for etf, name in US_SECTOR_ETFS.items():
        df = bars_map.get(etf, pd.DataFrame())
        st = _compute_return_and_sharpe(df)
        cur_p = float(df["close"].iloc[-1]) if not df.empty and "close" in df.columns else 0.0
        rs_1m = round(st["ret_1m"] - spy_1m, 2)
        rs_3m = round(st["ret_3m"] - spy_3m, 2)

        # 섹터 점수 계산 (기본 50점 + 1개월 RS * 2.5 + 3개월 RS * 1.5 + 추세 가산점)
        raw_score = 50.0 + (rs_1m * 2.5) + (rs_3m * 1.5) + (10.0 if st["is_uptrend"] else -5.0)
        sec_score = round(max(15.0, min(98.0, raw_score)), 1)

        if sec_score >= 70:
            label = "🔥 주도 섹터 (Overweight)"
        elif sec_score >= 52:
            label = "🟢 양호 (Neutral+)"
        else:
            label = "⚪ 소외/방어 (Underweight)"

        sector_items.append(
            USSectorLeaderItem(
                sector_etf=etf,
                sector_name=name,
                current_price=round(cur_p, 2),
                return_1m_pct=st["ret_1m"],
                return_3m_pct=st["ret_3m"],
                rel_strength_1m=rs_1m,
                rel_strength_3m=rs_3m,
                is_uptrend=st["is_uptrend"],
                sector_score=sec_score,
                regime_label=label,
            )
        )

    sector_items.sort(key=lambda x: x.sector_score, reverse=True)
    for idx, it in enumerate(sector_items, start=1):
        it.rank = idx

    return sector_items


async def _evaluate_single_us_candidate(
    item: Dict[str, Any],
    sector_map: Dict[str, USSectorLeaderItem],
    active_weights: Dict[str, float],
    recent_alerts_by_sym: Dict[str, Dict[str, Any]],
) -> Optional[USCandidateScoreItem]:
    """단일 미국 후보 종목에 대해 5대 멀티팩터(퀀트/컨센서스/섹터/밸류/뉴스) 점수를 계산한다."""
    sym = item["symbol"].strip().upper()
    name = item.get("name", sym)
    sector = item.get("sector", "글로벌 코어")
    sector_etf = item.get("sector_etf", "SPY")
    universe_tag = item.get("universe_tag", "US_CORE")
    exchange = item.get("exchange", "NASD")

    try:
        daily_task = get_us_daily_bars(sym, period="6mo")
        quote_task = get_us_quote(sym)
        cons_task = asyncio.to_thread(fetch_overseas_consensus, sym, exchange)

        daily_df, quote, cons = await asyncio.gather(
            daily_task, quote_task, cons_task, return_exceptions=True
        )
        if not isinstance(daily_df, pd.DataFrame):
            daily_df = pd.DataFrame()
        if not isinstance(quote, dict):
            quote = {}
        if not isinstance(cons, dict):
            cons = {}

        reports = cons.get("researches")
        if not isinstance(reports, list):
            try:
                reports = await asyncio.to_thread(fetch_overseas_research, sym)
            except Exception:
                reports = []
        if not isinstance(reports, list):
            reports = []

        cur_price = float(quote.get("price") or cons.get("last_close") or 0.0)
        if cur_price <= 0 and not daily_df.empty and "close" in daily_df.columns:
            cur_price = float(daily_df["close"].iloc[-1])
        change_1d = float(quote.get("change_pct") or 0.0)
        if change_1d == 0.0 and not daily_df.empty and "close" in daily_df.columns and len(daily_df) >= 2:
            c_last = float(daily_df["close"].iloc[-1])
            c_prev = float(daily_df["close"].iloc[-2])
            if c_prev > 0:
                change_1d = round(((c_last - c_prev) / c_prev) * 100.0, 2)

        q_stats = _compute_return_and_sharpe(daily_df)
        tech_res = compute_technical_signal(daily_df) if not daily_df.empty else {}
        ind_snap = extract_indicator_snapshot(daily_df) if not daily_df.empty else {}
        rsi_val = float(ind_snap.get("rsi") or 50.0)
        tech_dir = str(tech_res.get("direction") or "HOLD")
        vol_ratio = 1.0
        if not daily_df.empty and "volume" in daily_df.columns and len(daily_df) >= 20:
            vols = pd.to_numeric(daily_df["volume"], errors="coerce").dropna()
            if len(vols) >= 20 and vols.tail(20).mean() > 0:
                vol_ratio = float(vols.iloc[-1] / vols.tail(20).mean())

        # ------------------------------------------------------------------
        # 1. 퀀트 & 기술적 모멘텀 점수 (25점 만점)
        # ------------------------------------------------------------------
        quant_pts = 0.0
        # (a) 추세 정배열 (최대 8점)
        if q_stats["is_uptrend"]:
            quant_pts += 8.0
        elif q_stats["ret_1m"] > 0:
            quant_pts += 4.0

        # (b) 위험조정수익률 샤프지수 & 1/3개월 모멘텀 (최대 10점)
        sharpe = q_stats["sharpe"]
        if sharpe >= 1.8:
            quant_pts += 6.0
        elif sharpe >= 1.0:
            quant_pts += 4.5
        elif sharpe >= 0.3:
            quant_pts += 2.5

        if q_stats["ret_3m"] >= 15.0:
            quant_pts += 4.0
        elif q_stats["ret_3m"] >= 5.0:
            quant_pts += 2.5
        elif q_stats["ret_3m"] > 0:
            quant_pts += 1.0

        # (c) 기술적 시그널 & 수급 집중도 (최대 7점)
        if tech_dir == "BUY":
            quant_pts += 4.5
        elif tech_dir == "HOLD":
            quant_pts += 2.5
        if vol_ratio >= 1.3:
            quant_pts += 2.5
        elif vol_ratio >= 1.0:
            quant_pts += 1.0
        quant_pts = round(min(25.0, quant_pts), 1)

        # ------------------------------------------------------------------
        # 2. 월가 애널리스트 컨센서스 & 리서치 리포트 (25점 만점)
        # ------------------------------------------------------------------
        target_mean = cons.get("target_price_mean")
        recomm_mean = cons.get("recomm_mean") if cons.get("recomm_mean") is not None else cons.get("recommendation_mean")
        per_val = cons.get("per")
        pbr_val = cons.get("pbr")
        recomm_key = ""

        upside_pct = None
        if target_mean and cur_price > 0:
            upside_pct = round(((float(target_mean) - cur_price) / cur_price) * 100.0, 2)

        consensus_pts = 0.0
        if universe_tag == "US_CORE_ETF":
            # ETF는 개별 종목 목표주가가 부재하므로 지수/배당 안정성 기본 14.0점(56%) 부여
            consensus_pts = 14.0
            recomm_key = "core_etf"
        else:
            # (a) 목표주가 괴리율 Upside (최대 12점) - 6단계 차등화로 변별력 강화
            if upside_pct is not None:
                if upside_pct >= 40.0:
                    consensus_pts += 12.0     # 극대화된 저평가/낙폭과대 반등여력
                elif upside_pct >= 25.0:
                    consensus_pts += 10.0     # 강력한 상승 여력
                elif upside_pct >= 15.0:
                    consensus_pts += 8.0      # 양호한 상승 여력
                elif upside_pct >= 5.0:
                    consensus_pts += 5.5      # 적정 수준 상승 여력
                elif upside_pct >= 0.0:
                    consensus_pts += 3.0      # 목표주가 근접
                elif upside_pct >= -10.0:
                    consensus_pts += 1.5      # 소폭 고평가 (목표가 소폭 상회)
                else:
                    consensus_pts += 0.5      # 과열/심각한 고평가 (목표가 대비 +10% 이상 초과)
            else:
                consensus_pts += 4.0          # 목표가 데이터 부재 시 기본 4점

            # (b) 월가 투자의견 레이팅 (최대 10점) - 네이버/yfinance 스케일 정밀 분리
            if recomm_mean is not None:
                rm = float(recomm_mean)
                if rm <= 2.5:
                    # yfinance 스케일 (1.0 = Strong Buy ~ 5.0 = Strong Sell)
                    if rm <= 1.6:
                        consensus_pts += 10.0
                        recomm_key = "strong_buy"
                    elif rm <= 2.0:
                        consensus_pts += 7.5
                        recomm_key = "buy"
                    elif rm <= 2.4:
                        consensus_pts += 5.0
                        recomm_key = "moderate_buy"
                    else:
                        consensus_pts += 2.0
                        recomm_key = "hold"
                else:
                    # 네이버 스케일 (5.0점 만점 기준, 실측 3.5~4.4점 분포)
                    if rm >= 4.20:
                        consensus_pts += 10.0
                        recomm_key = "strong_buy"
                    elif rm >= 3.95:
                        consensus_pts += 7.5
                        recomm_key = "buy"
                    elif rm >= 3.60:
                        consensus_pts += 5.0
                        recomm_key = "moderate_buy"
                    else:
                        consensus_pts += 2.5
                        recomm_key = "hold"
            else:
                consensus_pts += 4.0

            # (c) 리서치 리포트 존재 가산점 (최대 3점)
            if len(reports) >= 3:
                consensus_pts += 3.0
            elif len(reports) == 2:
                consensus_pts += 2.0
            elif len(reports) == 1:
                consensus_pts += 1.0

        consensus_pts = round(min(25.0, consensus_pts), 1)
        latest_rep_title = reports[0].get("title", "") if reports else ""

        # ------------------------------------------------------------------
        # 3. 글로벌 판 & 유망 섹터 로테이션 점수 (20점 만점)
        # ------------------------------------------------------------------
        sec_info = sector_map.get(sector_etf)
        if sec_info:
            # 섹터 점수(15~98)를 20점 만점으로 스케일링
            sector_macro_pts = round(min(20.0, max(4.0, (sec_info.sector_score / 100.0) * 22.0)), 1)
        else:
            sector_macro_pts = 12.0

        # ------------------------------------------------------------------
        # 4. 펀더멘털 & 밸류에이션 점수 (15점 만점)
        # ------------------------------------------------------------------
        val_pts = 9.0  # 기본 중립 점수
        if universe_tag == "US_CORE_ETF":
            val_pts = 13.0
        elif per_val is not None and per_val > 0:
            if per_val <= 20.0:
                val_pts = 14.0
            elif per_val <= 35.0:
                val_pts = 11.5
            elif per_val <= 60.0:
                val_pts = 8.5
            else:
                val_pts = 6.0

        # 52주 고점 대비 건전한 눌림목 또는 신고가 돌파 가산
        high_52w = cons.get("w52_high") if cons.get("w52_high") is not None else cons.get("high_52w")
        if high_52w and cur_price > 0:
            dd_from_high = (cur_price - float(high_52w)) / float(high_52w)
            if -0.15 <= dd_from_high <= -0.03 and q_stats["is_uptrend"]:
                val_pts = min(15.0, val_pts + 1.5)
        val_pts = round(min(15.0, val_pts), 1)

        # ------------------------------------------------------------------
        # 5. 뉴스 속보 & AI Surprise 점수 (15점 만점)
        # ------------------------------------------------------------------
        alert_info = recent_alerts_by_sym.get(sym)
        news_title = ""
        news_surp = 0
        news_impact = "중립"
        news_pts = 8.0  # 뉴스 부재 시 중립 기본 8점

        if alert_info:
            news_title = alert_info.get("title", "")
            news_surp = int(alert_info.get("surprise_score") or 0)
            news_impact = alert_info.get("impact_type", "중립")
            if news_impact == "호재":
                if news_surp >= 75:
                    news_pts = 15.0
                elif news_surp >= 60:
                    news_pts = 12.5
                else:
                    news_pts = 10.5
            elif news_impact == "악재":
                if news_surp >= 75:
                    news_pts = 2.0
                else:
                    news_pts = 5.0

        total_score = round(quant_pts + consensus_pts + sector_macro_pts + val_pts + news_pts, 1)

        if total_score >= 78.0:
            grade = "S (최우선 주도주)"
        elif total_score >= 67.5:
            grade = "A (유망 편입 후보)"
        elif total_score >= 55.0:
            grade = "B (양호/관망)"
        else:
            grade = "C (비중축소/대기)"

        # 투자 포인트(Thesis) 자동 생성
        reasons: List[str] = []
        if sec_info and sec_info.sector_score >= 65:
            reasons.append(f"주도 섹터({sector})")
        if upside_pct is not None and upside_pct >= 12.0:
            reasons.append(f"월가 목표가 괴리율 +{upside_pct:.1f}%")
        elif recomm_key in ("strong_buy", "buy"):
            reasons.append(f"월가 투자의견 {recomm_key.upper()}")
        if q_stats["is_uptrend"] and sharpe >= 1.0:
            reasons.append(f"상승 추세(Sharpe {sharpe:.2f})")
        elif q_stats["ret_3m"] >= 10.0:
            reasons.append(f"3개월 모멘텀 +{q_stats['ret_3m']:.1f}%")
        if news_impact == "호재" and news_surp >= 65:
            reasons.append(f"뉴스 호재(Surprise {news_surp}점)")
        elif latest_rep_title:
            reasons.append("최신 리서치 커버리지")

        thesis_str = " · ".join(reasons) if reasons else f"{sector} 핵심 종목 (종합 {total_score}점)"

        is_held = sym in active_weights and active_weights[sym] > 0
        cur_w = float(active_weights.get(sym, 0.0))

        return USCandidateScoreItem(
            symbol=sym,
            name=name,
            sector=sector,
            sector_etf=sector_etf,
            universe_tag=universe_tag,
            exchange=exchange,
            current_price=round(cur_price, 2),
            change_1d_pct=round(change_1d, 2),
            return_1m_pct=q_stats["ret_1m"],
            return_3m_pct=q_stats["ret_3m"],
            sharpe_ratio=sharpe,
            volatility_pct=q_stats["vol"],
            rsi_14=round(rsi_val, 1),
            is_uptrend=q_stats["is_uptrend"],
            tech_signal_dir=tech_dir,
            target_mean_price=round(float(target_mean), 2) if target_mean else None,
            target_upside_pct=upside_pct,
            recommendation_key=recomm_key,
            recommendation_mean=round(float(recomm_mean), 2) if recomm_mean else None,
            per=round(float(per_val), 2) if per_val else None,
            pbr=round(float(pbr_val), 2) if pbr_val else None,
            report_count=len(reports),
            latest_report_title=latest_rep_title,
            latest_news_title=news_title,
            news_surprise_score=news_surp,
            news_impact_type=news_impact,
            quant_tech_pts=quant_pts,
            consensus_pts=consensus_pts,
            sector_macro_pts=sector_macro_pts,
            valuation_pts=val_pts,
            news_surprise_pts=news_pts,
            total_score=total_score,
            is_in_portfolio=is_held,
            current_target_weight=cur_w,
            grade=grade,
            thesis=thesis_str,
        )
    except Exception as e:
        logger.warning(f"미국 종목 '{sym}' 발굴 스코어링 오류: {e}")
        return None


async def score_us_discovery_universe(
    conn,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """미국장 후보 유니버스 전체를 5대 멀티팩터로 채점하여 랭킹 및 섹터 리더보드를 반환한다."""
    # 1. 캐시 확인
    if not force_refresh:
        raw_cache = await db.get_state(conn, STATE_US_DISCOVERY_CACHE)
        if raw_cache:
            try:
                cached = json.loads(raw_cache)
                scored_at = float(cached.get("scored_at") or 0.0)
                if (time.time() - scored_at) < US_DISCOVERY_CACHE_TTL_SEC and cached.get("candidates"):
                    # 현재 활성 포트폴리오 비중 상태만 최신으로 동기화
                    active_weights = await db.get_active_target_weights(conn)
                    for c in cached["candidates"]:
                        sym = c["symbol"]
                        c["is_in_portfolio"] = sym in active_weights and active_weights[sym] > 0
                        c["current_target_weight"] = float(active_weights.get(sym, 0.0))
                    return cached
            except Exception:
                pass

    # 2. 섹터 상대강도 리더보드 산출
    sector_leaders = await analyze_us_sector_leadership()
    sector_map = {s.sector_etf: s for s in sector_leaders}

    # 3. 후보 풀 로드 (시드 + 현재 활성 보유 종목 통합)
    seeds = load_us_seed_candidates()
    seed_map = {s["symbol"].upper(): s for s in seeds}

    active_weights = await db.get_active_target_weights(conn)
    for held_sym in active_weights:
        u_sym = held_sym.strip().upper()
        if u_sym.isalpha() and u_sym not in seed_map:
            seed_map[u_sym] = {
                "symbol": u_sym,
                "name": u_sym,
                "sector": "포트폴리오 편입 종목",
                "sector_etf": "SPY",
                "universe_tag": "US_PORTFOLIO",
                "exchange": "NASD",
            }

    # 4. 최근 뉴스 서프라이즈 알림 조회 (종목별 최신 1건 매핑)
    recent_alerts = await db.list_us_news_alerts(conn, limit=100)
    recent_alerts_by_sym: Dict[str, Dict[str, Any]] = {}
    for al in recent_alerts:
        s = (al.get("symbol") or "").upper()
        if s and s not in recent_alerts_by_sym:
            recent_alerts_by_sym[s] = al

    # 5. 병렬 배치 스코어링 (yfinance 과부하 방지를 위해 6개씩 청크 실행)
    candidate_list = list(seed_map.values())
    scored_items: List[USCandidateScoreItem] = []
    chunk_size = 6
    for i in range(0, len(candidate_list), chunk_size):
        chunk = candidate_list[i : i + chunk_size]
        res_list = await asyncio.gather(
            *(
                _evaluate_single_us_candidate(it, sector_map, active_weights, recent_alerts_by_sym)
                for it in chunk
            ),
            return_exceptions=True,
        )
        for r in res_list:
            if isinstance(r, USCandidateScoreItem):
                scored_items.append(r)
        await asyncio.sleep(0.15)

    scored_items.sort(key=lambda x: x.total_score, reverse=True)
    for idx, it in enumerate(scored_items, start=1):
        it.rank = idx

    # 신규 편입 추천 상위 후보 (현재 미보유 + 상위 12% 보수적 기준선 67.5점 이상 상위 5종목)
    top_new_picks = [
        it.model_dump() for it in scored_items
        if not it.is_in_portfolio and it.total_score >= DEFAULT_US_DISCOVERY_MIN_SCORE and it.universe_tag != "US_CORE_ETF"
    ][:5]

    payload = {
        "scored_at": time.time(),
        "total_evaluated": len(scored_items),
        "sector_leaders": [s.model_dump() for s in sector_leaders],
        "candidates": [it.model_dump() for it in scored_items],
        "top_new_picks": top_new_picks,
    }

    await db.set_state(conn, STATE_US_DISCOVERY_CACHE, json.dumps(payload, ensure_ascii=False))
    return payload


async def propose_us_discovery_portfolio_weights(
    conn,
    max_new_additions: int = 2,
    new_pick_weight_each: float = 0.08,
    min_discovery_score: float = DEFAULT_US_DISCOVERY_MIN_SCORE,
) -> USPortfolioEvolutionProposal:
    """현재의 '고정종목관리 + 리밸런싱' 포트폴리오를 초기값(Init)으로 삼아,
    발굴 엔진이 찾아낸 고득점(>=74점) 글로벌 유망 종목을 포트폴리오 목표 비중에 편입(ADD)하는
    스마트 자산배분 진화안을 생성한다.

    [안전 원칙]
    - 기존 코어 및 손실 보호 대상 보유 종목은 강제 퇴출(0% 청산)하지 않고 유지한다.
    - 신규 유망 종목(최대 max_new_additions개, 각 8% 기본 비중)을 추가하되,
      전체 비중 합계가 정확히 100%(1.0)가 되도록 기존 목표 비중을 비례 스케일링(희석)하여
      신규 달러 입금분이나 이익실현 재원이 자연스럽게 신규 주도주로 흘러가도록 설계한다.
    """
    from core.us_market.rebalance import DEFAULT_US_TARGET_WEIGHTS

    active_weights = await db.get_active_target_weights(conn)
    init_weights: Dict[str, float] = {}
    for sym, w in active_weights.items():
        if sym.strip().isalpha() and float(w or 0) > 0:
            init_weights[sym.strip().upper()] = float(w)

    if not init_weights:
        init_weights = dict(DEFAULT_US_TARGET_WEIGHTS)

    disc_data = await score_us_discovery_universe(conn, force_refresh=False)
    candidates = disc_data.get("candidates", [])
    cand_map = {c["symbol"]: c for c in candidates}

    # 미보유 종목 중 고득점 상위 후보 추출 (코어 ETF 제외, 개별 성장/주도주 우선)
    eligible_new = [
        c for c in candidates
        if c["symbol"] not in init_weights
        and float(c.get("total_score") or 0) >= min_discovery_score
        and c.get("universe_tag") != "US_CORE_ETF"
    ][:max_new_additions]

    if not eligible_new:
        # 만약 기준 점수 이상이 없으면 미보유 최상위 1종목이라도 후보로 제시
        fallback_new = [
            c for c in candidates
            if c["symbol"] not in init_weights and c.get("universe_tag") != "US_CORE_ETF"
        ][:1]
        eligible_new = fallback_new

    total_new_weight = round(len(eligible_new) * new_pick_weight_each, 4)
    scale_factor = max(0.5, 1.0 - total_new_weight)

    proposed: Dict[str, float] = {}
    for sym, w in init_weights.items():
        proposed[sym] = round(w * scale_factor, 4)

    added_syms: List[str] = []
    for c in eligible_new:
        s = c["symbol"]
        proposed[s] = round(new_pick_weight_each, 4)
        added_syms.append(s)

    # 합계 1.0 정밀 보정
    total_w = sum(proposed.values())
    if total_w > 0 and abs(total_w - 1.0) > 1e-5:
        first_key = next(iter(proposed))
        proposed[first_key] = round(proposed[first_key] + (1.0 - total_w), 4)

    items_detail: List[Dict[str, Any]] = []
    for sym, new_w in sorted(proposed.items(), key=lambda x: x[1], reverse=True):
        prev_w = init_weights.get(sym, 0.0)
        c_info = cand_map.get(sym, {})
        is_new = sym in added_syms
        items_detail.append({
            "symbol": sym,
            "name": c_info.get("name", sym),
            "sector": c_info.get("sector", "포트폴리오 코어"),
            "prev_weight": round(prev_w, 4),
            "new_weight": round(new_w, 4),
            "delta_weight": round(new_w - prev_w, 4),
            "total_score": c_info.get("total_score", 0.0),
            "grade": c_info.get("grade", "-"),
            "action": "✨ 신규 발굴 편입 (ADD)" if is_new else "🛡️ 코어 유지 (INIT)",
            "thesis": c_info.get("thesis", "기존 포트폴리오 코어 자산"),
        })

    if added_syms:
        added_desc = ", ".join(
            f"{s}({cand_map.get(s, {}).get('total_score', 0):.1f}점)" for s in added_syms
        )
        summary_reason = (
            f"기존 초기 포트폴리오({len(init_weights)}종목)를 코어로 유지하면서, "
            f"5대 멀티팩터 발굴 엔진이 포착한 글로벌 주도주 [{added_desc}]를 "
            f"각 {new_pick_weight_each*100:.0f}% 목표 비중으로 신규 편입(ADD) 제안합니다."
        )
    else:
        summary_reason = "현재 보유 포트폴리오가 최상위 점수를 유지 중이므로 기존 비중을 유지합니다."

    return USPortfolioEvolutionProposal(
        base_portfolio_count=len(init_weights),
        proposed_portfolio_count=len(proposed),
        added_symbols=added_syms,
        retained_symbols=list(init_weights.keys()),
        proposed_weights=proposed,
        items=items_detail,
        summary_reason=summary_reason,
    )
