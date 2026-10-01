# core/discovery_signals.py
"""종목 발굴 점수 계산용 순수 함수 (I/O 없음 - 단위 테스트 대상).

'하입 조기신호'는 예측이 아니다. 이미 관측된 비대칭 — 관심(뉴스/거래량/테마 동료)은
늘었는데 가격은 아직 덜 움직임 — 을 찾아 표시할 뿐이고, 그 비대칭이 해소되는 방향이
위일지 아래일지는 보장하지 않는다.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

DAY_SEC = 86400
NEWS_RECENT_DAYS = 3
NEWS_BASELINE_DAYS = 10

EARLY_NEWS_ACCEL_MIN = 2.0
EARLY_NEWS_MAX_RET_5D = 0.05
EARLY_VOL_RATIO_MIN = 1.8
EARLY_VOL_MAX_RET_5D = 0.04
EARLY_THEME_MAX_RET_5D = 0.03

THEME_MIN_MOVERS = 2
THEME_MAX_LAGGARDS = 3
THEME_MAX_MEMBERS = 80  # 'OO 관련주 전체' 같은 과대 테마는 동반 상승 신호로서 의미가 없다
THEME_NAME_BLOCKLIST = ("신규 상장", "신규상장")  # 사업 테마가 아니라 상장연도 묶음

ANGLE_BY_COMPONENT = {"tech": "momentum", "value": "value", "early": "early", "broker": "broker"}
ANGLE_LABELS = {"momentum": "모멘텀", "value": "저평가", "early": "하입 조기신호", "broker": "증권사 추천"}


def news_acceleration(published_ats: List[float], now: float, truncated: bool) -> Dict[str, Any]:
    """최근 3일 일평균 기사 수 / 그 이전(최대 7일) 일평균 기사 수.

    truncated=True(요청한 개수만큼 꽉 차게 받음)면 가장 오래된 기사 이전 구간은 관측하지
    못한 것이므로 기준 구간을 관측된 범위로 줄인다 — 안 그러면 기사가 많은 종목일수록
    기준 구간이 비어 보여 가속도가 과대평가된다."""
    recent_start = now - NEWS_RECENT_DAYS * DAY_SEC
    baseline_start = now - NEWS_BASELINE_DAYS * DAY_SEC
    if truncated and published_ats:
        baseline_start = max(baseline_start, min(published_ats))

    recent = sum(1 for ts in published_ats if ts >= recent_start)
    prior = sum(1 for ts in published_ats if baseline_start <= ts < recent_start)
    result: Dict[str, Any] = {"news_3d": recent}

    prior_days = (recent_start - baseline_start) / DAY_SEC
    if prior_days < 1:
        return result
    prior_daily = prior / prior_days
    # 기준 구간에 기사가 0건이어도 무한대가 되지 않게 하루 0.3건을 바닥값으로 둔다
    result["news_accel"] = round((recent / NEWS_RECENT_DAYS) / max(prior_daily, 0.3), 2)
    return result


def volume_accumulation(df: pd.DataFrame) -> Dict[str, Any]:
    """df: indicators.chart_rows_to_dataframe 결과 (date 오름차순, close/volume 컬럼)."""
    if df is None or len(df) < 21:
        return {}
    volume = df["volume"].astype(float)
    close = df["close"].astype(float)
    avg20 = volume.iloc[-20:].mean()
    if avg20 <= 0 or close.iloc[-6] <= 0:
        return {}
    return {
        "vol_ratio_5_20": round(volume.iloc[-5:].mean() / avg20, 2),
        "ret_5d_pct": round((close.iloc[-1] / close.iloc[-6] - 1) * 100, 2),
    }


def theme_laggards(
    memberships: Iterable[Dict[str, str]],
    movers: Dict[str, float],
    eligible: Dict[str, Dict[str, Any]],
    names: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """오늘 같은 테마 종목이 THEME_MIN_MOVERS개 이상 급등했는데 아직 급등 목록에 없는 테마 동료.

    movers: {symbol: 당일 등락률(%)}. eligible: 발굴 가능한 종목의 stock_master 행 (시총 기준
    상위 THEME_MAX_LAGGARDS개만 뽑는다 - 유동성이 없는 테마 끝자락 종목은 제외). names: 전종목
    이름 (급등 동료는 소형주라 eligible 밖일 수 있다)."""
    themes: Dict[str, Dict[str, Any]] = {}
    for m in memberships:
        theme = themes.setdefault(m["theme_code"], {"name": m["theme_name"], "members": []})
        theme["members"].append(m["symbol"])

    result: Dict[str, Dict[str, Any]] = {}
    for theme in themes.values():
        members = theme["members"]
        if len(members) > THEME_MAX_MEMBERS or any(word in theme["name"] for word in THEME_NAME_BLOCKLIST):
            continue
        theme_movers = [s for s in members if s in movers]
        if len(theme_movers) < THEME_MIN_MOVERS:
            continue
        laggards = sorted(
            (s for s in members if s not in movers and s in eligible),
            key=lambda s: eligible[s].get("market_cap_eok") or 0,
            reverse=True,
        )[:THEME_MAX_LAGGARDS]
        mover_names = [names.get(s, s) for s in theme_movers]
        for symbol in laggards:
            prev = result.get(symbol)
            if prev is None or len(theme_movers) > len(prev["movers"]):
                result[symbol] = {"theme": theme["name"], "movers": mover_names[:5]}
    return result


def early_signal(entry: Dict[str, Any]) -> Tuple[float, List[str]]:
    """(0..1 점수, 근거 목록). entry에 news_accel/vol_ratio_5_20/ret_5d_pct/theme_laggard가
    있으면 사용한다."""
    ret_5d = abs(entry.get("ret_5d_pct") or 0) / 100
    score = 0.0
    reasons: List[str] = []

    accel = entry.get("news_accel")
    if accel is not None and accel >= EARLY_NEWS_ACCEL_MIN and ret_5d < EARLY_NEWS_MAX_RET_5D:
        score += min(1.0, (accel - 1) / 3)
        reasons.append(f"최근 3일 뉴스가 평소의 {accel:.1f}배인데 5일 주가변동은 {entry.get('ret_5d_pct', 0):+.1f}%")

    vol_ratio = entry.get("vol_ratio_5_20")
    if vol_ratio is not None and vol_ratio >= EARLY_VOL_RATIO_MIN and ret_5d < EARLY_VOL_MAX_RET_5D:
        score += min(1.0, (vol_ratio - 1) / 2)
        reasons.append(f"5일 평균 거래량이 20일 평균의 {vol_ratio:.1f}배인데 가격은 거의 그대로 (매집 가능성)")

    laggard = entry.get("theme_laggard")
    if laggard and ret_5d < EARLY_THEME_MAX_RET_5D:
        score += 0.5
        reasons.append(f"'{laggard['theme']}' 테마 동료({', '.join(laggard['movers'][:3])})는 급등, 이 종목은 아직 미반영")

    return min(1.0, score), reasons


def group_research_reports(
    reports: List[Dict[str, Any]], lookback_days: int, now: datetime,
) -> Dict[str, Dict[str, Any]]:
    """naver_research.fetch_company_research 결과를 종목별로 묶는다 (최신순 유지)."""
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y%m%d")
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in reports:
        if r["date"] < cutoff:
            continue
        ev = grouped.setdefault(r["symbol"], {"days": lookback_days, "report_count": 0, "reports": []})
        ev["report_count"] += 1
        ev["reports"].append({k: r[k] for k in ("broker", "title", "date", "read_count")})
    return grouped


def broker_score(reports: List[Dict[str, Any]]) -> float:
    brokers = {r.get("broker") for r in reports if r.get("broker")}
    return min(1.0, len(brokers) / 3)


def finalize_scores(entries: List[Dict[str, Any]], weights: Dict[str, float]) -> None:
    """entry["raw_components"]를 후보 풀 안의 백분위로 바꾼 뒤 가중합한다 (in-place).

    절대값을 그대로 쓰면 포화되는 컴포넌트가 숏리스트를 독식한다 — 예: 컨센서스 목표가가 시장 전반적으로
    현재가보다 크게 높을 때 밸류에이션 BUY 강도가 대부분 종목에서 0.8 이상이 된다. 0(해당 없음)은
    백분위를 매기지 않고 0으로 둔다."""
    for key in weights:
        positive = sorted({e["raw_components"].get(key, 0.0) for e in entries if e["raw_components"].get(key, 0.0) > 0})
        rank = {v: (i + 1) / len(positive) for i, v in enumerate(positive)}
        for e in entries:
            raw = e["raw_components"].get(key, 0.0)
            e.setdefault("components", {})[key] = round(rank.get(raw, 0.0), 3) if raw > 0 else 0.0
    for e in entries:
        e["score"], e["angle"] = composite_score(e["components"], weights)
        e["angle_label"] = ANGLE_LABELS[e["angle"]]


def composite_score(components: Dict[str, float], weights: Dict[str, float]) -> Tuple[float, str]:
    """(가중합 점수, 가장 크게 기여한 관점)."""
    weighted = {k: components.get(k, 0.0) * w for k, w in weights.items()}
    total = sum(weighted.values())
    top_component = max(weighted, key=weighted.get) if total > 0 else "tech"
    return round(total, 4), ANGLE_BY_COMPONENT.get(top_component, "momentum")


def select_with_quota(
    entries: List[Dict[str, Any]], top_n: int, quota: Dict[str, int], exclude: Optional[set] = None,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """관점별 할당량을 점수순으로 먼저 채우고 남는 자리는 전체 점수순. 점수가 min_score 미만이거나
    0인 종목은 뽑지 않는다 (top_n을 못 채우면 채우지 않고 적게 반환)."""
    exclude = exclude or set()
    pool = sorted(
        (e for e in entries if e["symbol"] not in exclude and e.get("score", 0) > 0 and e.get("score", 0) >= min_score),
        key=lambda e: e["score"], reverse=True,
    )
    chosen: List[Dict[str, Any]] = []
    chosen_symbols: set = set()
    for angle, count in quota.items():
        for e in [e for e in pool if e.get("angle") == angle][:count]:
            if len(chosen) >= top_n:
                break
            chosen.append(e)
            chosen_symbols.add(e["symbol"])
    for e in pool:
        if len(chosen) >= top_n:
            break
        if e["symbol"] not in chosen_symbols:
            chosen.append(e)
            chosen_symbols.add(e["symbol"])
    return sorted(chosen, key=lambda e: e["score"], reverse=True)


def build_thesis(entry: Dict[str, Any], early_reasons: List[str]) -> List[str]:
    """LLM과 대시보드에 보여줄 한국어 발굴 근거 (최대 4줄)."""
    lines: List[str] = []
    sources = entry.get("sources") or {}

    if "broker" in sources:
        ev = sources["broker"]
        titles = " / ".join(f"{r['broker']} '{r['title']}'" for r in ev.get("reports", [])[:2])
        lines.append(f"최근 {ev.get('days')}일 증권사 리포트 {ev.get('report_count')}건: {titles}")
    gap = entry.get("target_gap_pct")
    if gap is not None and gap >= 0.10:
        per = entry.get("per")
        lines.append(
            f"컨센서스 목표가 대비 상승여력 {gap:.0%}" + (f", PER {per:.1f}" if per else "")
        )
    lines.extend(early_reasons)
    tech = entry.get("tech") or {}
    if tech.get("direction") == "BUY" and tech.get("strength", 0) >= 0.5:
        lines.append(f"일봉 기술적 매수 시그널(RSI/MACD/볼린저) 강도 {tech['strength']:.2f}")
    if "momentum" in sources:
        ev = sources["momentum"]
        parts = []
        if ev.get("rise_pct") is not None:
            parts.append(f"당일 {ev['rise_pct']:+.1f}%")
        if ev.get("vol_increase_pct") is not None:
            parts.append(f"거래량 전일 대비 {ev['vol_increase_pct']:+.0f}%")
        if parts:
            lines.append("오늘 급등/거래 급증 순위 진입 (" + ", ".join(parts) + ")")
    if "news" in sources:
        ev = sources["news"]
        lines.append(f"시장 뉴스 헤드라인 표본에서 {ev.get('mentions')}회 언급")
    return lines[:4]
