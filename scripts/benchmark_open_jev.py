#!/usr/bin/env python3
"""Open-Jev 계열 로컬 System-1 모델 (Laya vs Clef vs Von) 속도·정확도 A/B 벤치마크.

RTX 3090 (24GB VRAM) 환경에서 Finance_Jarvis의 핵심 의사결정 태스크인
`News Veto` (BUY / SELL / HOLD) 및 한국어/영문 금융 뉴스 분류 성능을 비교한다.

지원 실행 모드:
  1. HTTP `/v1/systemone` 모드 (각 모델이 로컬 서버로 떠 있을 때):
     uv run python scripts/benchmark_open_jev.py \
       --endpoints laya=http://127.0.0.1:8011,von=http://127.0.0.1:8012,clef=http://127.0.0.1:8013
  2. 인프로세스 HuggingFace 모드 (torch + transformers 환경에서 직접 로드):
     .venv-bench/bin/python scripts/benchmark_open_jev.py --models laya,von,clef-flash
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.llm.decision_provider import HttpSystemOneDecisionProvider

# 한국어(국장) + 영문(미장) 금융 뉴스 골든 평가셋 (SELL=악재 Veto 대상, BUY=강한 호재, HOLD=일상/중립)
GOLDEN_DATASET: List[Dict[str, str]] = [
    # --- KR (한국어) SELL (악재 Veto 필수) ---
    {"lang": "KR", "symbol": "005930", "label": "SELL", "title": "삼성전자 3분기 영업이익 시장 기대치 대폭 하회 어닝쇼크", "summary": "반도체 부문 적자 확대와 수율 부진으로 컨센서스를 30% 밑돌았다."},
    {"lang": "KR", "symbol": "035720", "label": "SELL", "title": "카카오 경영진 배임·횡령 혐의로 검찰 압수수색 진행", "summary": "사법 리스크 부각에 기관 및 외국인 대량 매도세 출회."},
    {"lang": "KR", "symbol": "000660", "label": "SELL", "title": "미국 상무부 대중국 HBM 수출 전면 금지 조치 발표", "summary": "핵심 매출처 수출 제한으로 향후 실적 타격 불가피."},
    {"lang": "KR", "symbol": "051910", "label": "SELL", "title": "LG화학 대규모 유상증자 결정에 주주가치 희석 우려 급락", "summary": "기존 주식 수 대비 20% 규모의 신주 발행으로 투자심리 급랭."},
    {"lang": "KR", "symbol": "005380", "label": "SELL", "title": "현대차 북미 전기차 대규모 리콜 명령 및 공장 가동 중단", "summary": "배터리 화재 결함 조사로 수십만 대 리콜 비용 반영 예정."},
    {"lang": "KR", "symbol": "068270", "label": "SELL", "title": "셀트리온 핵심 바이오시밀러 FDA 품목허가 최종 거절", "summary": "임상 데이터 보완 요구로 출시 일정 최소 1년 이상 지연."},
    {"lang": "KR", "symbol": "247540", "label": "SELL", "title": "에코프로비엠 2분기 영업손실 전환 및 가이던스 대폭 하향", "summary": "전기차 수요 둔화로 양극재 출하량 급감."},
    {"lang": "KR", "symbol": "035420", "label": "SELL", "title": "네이버 공정위 독과점 제재 및 역대 최대 과징금 부과", "summary": "검색 알고리즘 우대 혐의로 수천억 원대 과징금 및 시정명령."},
    # --- KR (한국어) BUY (호재) ---
    {"lang": "KR", "symbol": "000660", "label": "BUY", "title": "SK하이닉스 엔비디아 HBM4 단독 대량 공급 계약 체결", "summary": "차세대 AI 가속기 핵심 물량 독점 수주로 사상 최대 실적 전망."},
    {"lang": "KR", "symbol": "012450", "label": "BUY", "title": "한화에어로스페이스 유럽 10조원 규모 대규모 방산 수출 계약", "summary": "연간 매출을 웃도는 초대형 수주로 향후 5년 일감 확보."},
    {"lang": "KR", "symbol": "005930", "label": "BUY", "title": "삼성전자 10조원 규모 자사주 매입 및 전량 소각 결정", "summary": "강력한 주주환원 정책 발표로 외국인 대규모 순매수 유입."},
    {"lang": "KR", "symbol": "329180", "label": "BUY", "title": "HD현대중공업 2분기 영업이익 컨센서스 45% 상회 어닝서프라이즈", "summary": "고선가 LNG선 인도 본격화로 마진율 급개선."},
    {"lang": "KR", "symbol": "207940", "label": "BUY", "title": "삼성바이오로직스 글로벌 빅파마와 2조원대 역대 최대 위탁생산 계약", "summary": "5공장 가동률 조기 상승 및 연간 매출 가이던스 상향."},
    {"lang": "KR", "symbol": "005380", "label": "BUY", "title": "현대차 미국 하이브리드 판매 사상 최대치 경신 및 배당 확대", "summary": "고수익 차종 믹스 개선으로 사상 최고 영업이익률 달성."},
    # --- KR (한국어) HOLD (일상/중립) ---
    {"lang": "KR", "symbol": "005930", "label": "HOLD", "title": "삼성전자 차세대 가전 신제품 국내 체험 행사 개최", "summary": "서울 강남구 플래그십 스토어에서 소비자 대상 전시회 진행."},
    {"lang": "KR", "symbol": "035420", "label": "HOLD", "title": "네이버 다음 주 정기 주주총회 소집 공고", "summary": "통상적인 사외이사 선임 및 재무제표 승인 안건 상정."},
    {"lang": "KR", "symbol": "005380", "label": "HOLD", "title": "현대차 ESG 경영 보고서 발간 및 탄소중립 로드맵 소개", "summary": "연례 지속가능경영 성과 정리 자료 배포."},
    {"lang": "KR", "symbol": "051910", "label": "HOLD", "title": "LG화학 석유화학 업황 점검 세미나 참석", "summary": "업계 관계자들과 중장기 친환경 소재 트렌드 논의."},
    {"lang": "KR", "symbol": "000660", "label": "HOLD", "title": "SK하이닉스 하반기 대졸 신입사원 정기 채용 시작", "summary": "매년 실시하는 정기 공개채용 일정 공지."},
    {"lang": "KR", "symbol": "035720", "label": "HOLD", "title": "카카오 이모티콘 출시 15주년 기념 이벤트 진행", "summary": "이용자 대상 기념 프로모션 실시."},

    # --- US (영문) SELL (악재 Veto 필수) ---
    {"lang": "US", "symbol": "NVDA", "label": "SELL", "title": "DOJ issues antitrust subpoena to Nvidia escalating probe into AI chip dominance", "summary": "Regulators investigate whether Nvidia penalizes buyers who switch to rivals."},
    {"lang": "US", "symbol": "TSLA", "label": "SELL", "title": "Tesla misses Q3 delivery estimates badly and cuts gross margin outlook", "summary": "Aggressive price cuts failed to offset slowing EV demand across Europe and China."},
    {"lang": "US", "symbol": "PLTR", "label": "SELL", "title": "US Defense Department cancels major Palantir software contract amid budget review", "summary": "Loss of flagship government contract triggers sharp downward revenue revision."},
    {"lang": "US", "symbol": "AAPL", "label": "SELL", "title": "China bans government officials and state enterprises from using iPhones", "summary": "Escalating geopolitical restrictions threaten Apple's second-largest market."},
    {"lang": "US", "symbol": "SMCI", "label": "SELL", "title": "Auditor resigns from Super Micro Computer citing accounting and internal control concerns", "summary": "Shares plunge over 30% on delisting and SEC investigation fears."},
    {"lang": "US", "symbol": "AVGO", "label": "SELL", "title": "Broadcom guides next-quarter revenue well below Wall Street consensus on weak enterprise demand", "summary": "Management flags sharp inventory digestion across networking customers."},
    {"lang": "US", "symbol": "META", "label": "SELL", "title": "EU fines Meta $1.3 billion and orders suspension of EU-US user data transfers", "summary": "Landmark privacy ruling imposes heavy compliance costs and ad-targeting limits."},
    {"lang": "US", "symbol": "NVDA", "label": "SELL", "title": "US Commerce Department imposes immediate export ban on Nvidia data center GPUs", "summary": "New licensing rules halt billions in backlog shipments overnight."},
    # --- US (영문) BUY (호재) ---
    {"lang": "US", "symbol": "NVDA", "label": "BUY", "title": "Nvidia smashes Q4 earnings estimates and raises guidance by $4B on surging Blackwell demand", "summary": "Data center revenue doubled year-over-year with expanding gross margins."},
    {"lang": "US", "symbol": "PLTR", "label": "BUY", "title": "Palantir wins $1.2B multi-year US Army AI platform contract and joins S&P 500", "summary": "Commercial and defense AI adoption accelerates above Street forecasts."},
    {"lang": "US", "symbol": "AVGO", "label": "BUY", "title": "Broadcom announces massive custom AI ASIC deal with two hyperscalers", "summary": "Multi-year silicon roadmap lifts projected AI revenue by 60%."},
    {"lang": "US", "symbol": "TSLA", "label": "BUY", "title": "Tesla receives full regulatory approval for FSD unsupervised robotaxi launch", "summary": "Breakthrough autonomous permit unlocks high-margin recurring fleet revenue."},
    {"lang": "US", "symbol": "MSFT", "label": "BUY", "title": "Microsoft Azure AI growth accelerates sharply beating consensus revenue and EPS", "summary": "Enterprise Copilot seat additions exceed expectations across all tiers."},
    {"lang": "US", "symbol": "AAPL", "label": "BUY", "title": "Apple announces historic $110B stock buyback and beats iPhone revenue expectations", "summary": "Services margin hits all-time high alongside strong upgrade cycle."},
    # --- US (영문) HOLD (일상/중립) ---
    {"lang": "US", "symbol": "NVDA", "label": "HOLD", "title": "Nvidia shares trade sideways ahead of next week's developer keynote", "summary": "Investors await routine product roadmap updates with no major surprises expected."},
    {"lang": "US", "symbol": "SCHD", "label": "HOLD", "title": "Why dividend ETFs like SCHD remain popular among long-term retirement investors", "summary": "Opinion column reviewing historical yield and expense ratios."},
    {"lang": "US", "symbol": "SGOV", "label": "HOLD", "title": "Short-term Treasury ETF SGOV declares routine monthly distribution", "summary": "Distribution aligns with prevailing 0-3 month T-bill yields."},
    {"lang": "US", "symbol": "VOO", "label": "HOLD", "title": "S&P 500 index closes 0.1% higher in quiet pre-holiday trading session", "summary": "Light volume across major US equity benchmarks with few catalysts."},
    {"lang": "US", "symbol": "AAPL", "label": "HOLD", "title": "Apple opens redesigned retail store in downtown Chicago", "summary": "Routine retail footprint refresh with standard community workshops."},
    {"lang": "US", "symbol": "PLTR", "label": "HOLD", "title": "Palantir schedules Q3 earnings conference call for next month", "summary": "Standard investor relations calendar announcement."},
]

HF_REPO_MAP: Dict[str, str] = {
    "laya": "convaiinnovations/laya",
    "von": "wfzyx/von",
    "clef-flash": "Cloudflare/clef-flash",
    "clef": "Cloudflare/clef",
}


@dataclass
class BenchmarkMetrics:
    model_name: str
    backend: str
    load_time_sec: float
    vram_peak_mb: float
    samples: int
    p50_ms: float
    p95_ms: float
    overall_acc: float
    kr_acc: float
    us_acc: float
    macro_f1: float
    veto_recall_all: float
    veto_precision_all: float
    veto_recall_kr: float
    veto_recall_us: float
    composite_score: float
    error: Optional[str] = None


def compute_metrics(
    model_name: str,
    backend: str,
    load_time_sec: float,
    vram_peak_mb: float,
    records: List[Dict[str, Any]],
) -> BenchmarkMetrics:
    labels = ("BUY", "SELL", "HOLD")
    total = len(records)
    correct = sum(1 for r in records if r["pred"] == r["gold"])
    kr_recs = [r for r in records if r["lang"] == "KR"]
    us_recs = [r for r in records if r["lang"] == "US"]
    kr_acc = sum(1 for r in kr_recs if r["pred"] == r["gold"]) / max(1, len(kr_recs))
    us_acc = sum(1 for r in us_recs if r["pred"] == r["gold"]) / max(1, len(us_recs))

    f1s: List[float] = []
    for lb in labels:
        tp = sum(1 for r in records if r["pred"] == lb and r["gold"] == lb)
        fp = sum(1 for r in records if r["pred"] == lb and r["gold"] != lb)
        fn = sum(1 for r in records if r["pred"] != lb and r["gold"] == lb)
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1s.append((2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0)
    macro_f1 = sum(f1s) / len(labels)

    def _sell_stats(subset: List[Dict[str, Any]]) -> Tuple[float, float]:
        tp = sum(1 for r in subset if r["pred"] == "SELL" and r["gold"] == "SELL")
        fp = sum(1 for r in subset if r["pred"] == "SELL" and r["gold"] != "SELL")
        fn = sum(1 for r in subset if r["pred"] != "SELL" and r["gold"] == "SELL")
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        return rec, prec

    veto_rec_all, veto_prec_all = _sell_stats(records)
    veto_rec_kr, _ = _sell_stats(kr_recs)
    veto_rec_us, _ = _sell_stats(us_recs)

    latencies = sorted(r["latency_ms"] for r in records)
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0.0

    # 종합 점수: 악재 Veto Recall(40%) + Macro F1(35%) + 속도 점수(25%, 50ms 이하 만점)
    speed_score = max(0.0, min(1.0, 1.0 - max(0.0, p95 - 50.0) / 500.0))
    composite = 0.40 * veto_rec_all + 0.35 * macro_f1 + 0.25 * speed_score

    return BenchmarkMetrics(
        model_name=model_name,
        backend=backend,
        load_time_sec=round(load_time_sec, 2),
        vram_peak_mb=round(vram_peak_mb, 1),
        samples=total,
        p50_ms=round(p50, 2),
        p95_ms=round(p95, 2),
        overall_acc=round(correct / max(1, total), 4),
        kr_acc=round(kr_acc, 4),
        us_acc=round(us_acc, 4),
        macro_f1=round(macro_f1, 4),
        veto_recall_all=round(veto_rec_all, 4),
        veto_precision_all=round(veto_prec_all, 4),
        veto_recall_kr=round(veto_rec_kr, 4),
        veto_recall_us=round(veto_rec_us, 4),
        composite_score=round(composite, 4),
    )


async def benchmark_http_endpoint(name: str, endpoint: str) -> BenchmarkMetrics:
    provider = HttpSystemOneDecisionProvider(endpoint=endpoint, model_id=name, timeout_sec=5.0)
    records: List[Dict[str, Any]] = []
    t_load = 0.0
    try:
        for item in GOLDEN_DATASET:
            res = await provider.classify_news_veto(item["symbol"], item["title"], item["summary"])
            records.append({
                "lang": item["lang"],
                "gold": item["label"],
                "pred": res.label,
                "latency_ms": res.latency_ms,
            })
        return compute_metrics(name, f"http:{endpoint}", t_load, 0.0, records)
    except Exception as e:
        return BenchmarkMetrics(
            model_name=name, backend=f"http:{endpoint}", load_time_sec=0.0, vram_peak_mb=0.0,
            samples=0, p50_ms=0.0, p95_ms=0.0, overall_acc=0.0, kr_acc=0.0, us_acc=0.0,
            macro_f1=0.0, veto_recall_all=0.0, veto_precision_all=0.0,
            veto_recall_kr=0.0, veto_recall_us=0.0, composite_score=0.0, error=str(e),
        )


CHOICE_CRITERIA: Dict[str, str] = {
    "BUY": "Strong positive catalyst for the stock price (earnings beat, major contract, buyback, upgrade).",
    "SELL": "Severe negative bad news that risks a sharp stock drop (earnings miss, lawsuit, probe, export ban, recall, fraud, dilution).",
    "HOLD": "Routine, neutral, or opinion news with minimal immediate stock price impact.",
}


def _extract_laya_choice(res: Any) -> str:
    if isinstance(res, dict):
        answers = res.get("answers") if isinstance(res.get("answers"), dict) else res
        sent = answers.get("sentiment")
        if isinstance(sent, dict):
            ans = sent.get("choice") or sent.get("answer") or sent.get("label") or sent.get("value")
            if ans:
                return str(ans).upper()
            probs = sent.get("probabilities") or sent.get("probs")
            if isinstance(probs, dict) and probs:
                return str(max(probs.items(), key=lambda kv: kv[1])[0]).upper()
        elif isinstance(sent, str):
            return sent.upper()
    return "HOLD"


def benchmark_laya(variant: str) -> BenchmarkMetrics:
    """Native Laya SDK 벤치마크 ('laya-en' 또는 'laya-multilingual')."""
    import torch
    import laya

    device = "cuda" if torch.cuda.is_available() else "cpu"
    subfolder = "multilingual" if variant == "laya-multilingual" else None
    backend = f"laya:convaiinnovations/laya({subfolder or 'english'})"
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    try:
        agent = laya.load("convaiinnovations/laya", subfolder=subfolder, device=device)
        load_sec = time.perf_counter() - t0
    except Exception as e:
        return BenchmarkMetrics(
            model_name=variant, backend=backend, load_time_sec=round(time.perf_counter() - t0, 2),
            vram_peak_mb=0.0, samples=0, p50_ms=0.0, p95_ms=0.0, overall_acc=0.0, kr_acc=0.0, us_acc=0.0,
            macro_f1=0.0, veto_recall_all=0.0, veto_precision_all=0.0,
            veto_recall_kr=0.0, veto_recall_us=0.0, composite_score=0.0, error=str(e),
        )

    questions = {
        "sentiment": {
            "type": "choice",
            "instructions": "Classify the immediate stock price impact of this financial news.",
            "criteria": CHOICE_CRITERIA,
        }
    }
    # Warmup 1회
    _ = agent.predict("Warmup stock news.", questions)
    if device == "cuda":
        torch.cuda.synchronize()

    records: List[Dict[str, Any]] = []
    for item in GOLDEN_DATASET:
        state = f"Symbol: {item['symbol']}\nTitle: {item['title']}\nSummary: {item['summary']}"
        ts = time.perf_counter()
        out = agent.predict(state, questions)
        if device == "cuda":
            torch.cuda.synchronize()
        ms = (time.perf_counter() - ts) * 1000.0
        records.append({
            "lang": item["lang"],
            "gold": item["label"],
            "pred": _extract_laya_choice(out),
            "latency_ms": ms,
        })

    vram_mb = (torch.cuda.max_memory_allocated() / (1024 * 1024)) if device == "cuda" else 0.0
    del agent
    if device == "cuda":
        torch.cuda.empty_cache()
    return compute_metrics(variant, backend, load_sec, vram_mb, records)


def benchmark_von() -> BenchmarkMetrics:
    """Native von-sdk 벤치마크 (`wfzyx/von`)."""
    import torch
    import von

    device = "cuda" if torch.cuda.is_available() else "cpu"
    backend = f"von-sdk:wfzyx/von({device})"
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    try:
        # 첫 호출에서 모델 로드
        _ = von.decide(
            state="Warmup stock news.",
            choices=CHOICE_CRITERIA,
            instructions="Classify the immediate stock price impact of this financial news.",
        )
        if device == "cuda":
            torch.cuda.synchronize()
        load_sec = time.perf_counter() - t0
    except Exception as e:
        return BenchmarkMetrics(
            model_name="von", backend=backend, load_time_sec=round(time.perf_counter() - t0, 2),
            vram_peak_mb=0.0, samples=0, p50_ms=0.0, p95_ms=0.0, overall_acc=0.0, kr_acc=0.0, us_acc=0.0,
            macro_f1=0.0, veto_recall_all=0.0, veto_precision_all=0.0,
            veto_recall_kr=0.0, veto_recall_us=0.0, composite_score=0.0, error=str(e),
        )

    records: List[Dict[str, Any]] = []
    for item in GOLDEN_DATASET:
        state = f"Symbol: {item['symbol']}\nTitle: {item['title']}\nSummary: {item['summary']}"
        ts = time.perf_counter()
        ans = von.decide(
            state=state,
            choices=CHOICE_CRITERIA,
            instructions="Classify the immediate stock price impact of this financial news.",
        )
        if device == "cuda":
            torch.cuda.synchronize()
        ms = (time.perf_counter() - ts) * 1000.0
        pred = str(getattr(ans, "answer", None) or getattr(ans, "choice", None) or getattr(ans, "value", "HOLD")).upper()
        if pred not in ("BUY", "SELL", "HOLD"):
            pred = "HOLD"
        records.append({
            "lang": item["lang"],
            "gold": item["label"],
            "pred": pred,
            "latency_ms": ms,
        })

    vram_mb = (torch.cuda.max_memory_allocated() / (1024 * 1024)) if device == "cuda" else 0.0
    if device == "cuda":
        torch.cuda.empty_cache()
    return compute_metrics("von", backend, load_sec, vram_mb, records)


def benchmark_clef_flash() -> BenchmarkMetrics:
    """Native Cloudflare/clef-flash (9B) 벤치마크."""
    import torch
    from huggingface_hub import snapshot_download

    device = "cuda" if torch.cuda.is_available() else "cpu"
    backend = f"clef:Cloudflare/clef-flash({device})"
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    try:
        path = snapshot_download("Cloudflare/clef-flash")
        if path not in sys.path:
            sys.path.insert(0, path)
        from joint_schema_model import collate_records, encode_record, load_release_model

        model, processor = load_release_model(path, device=device)
        load_sec = time.perf_counter() - t0
    except Exception as e:
        return BenchmarkMetrics(
            model_name="clef-flash", backend=backend, load_time_sec=round(time.perf_counter() - t0, 2),
            vram_peak_mb=0.0, samples=0, p50_ms=0.0, p95_ms=0.0, overall_acc=0.0, kr_acc=0.0, us_acc=0.0,
            macro_f1=0.0, veto_recall_all=0.0, veto_precision_all=0.0,
            veto_recall_kr=0.0, veto_recall_us=0.0, composite_score=0.0, error=str(e),
        )

    records: List[Dict[str, Any]] = []
    for item in GOLDEN_DATASET:
        record = {
            "state": {"symbol": item["symbol"], "title": item["title"], "summary": item["summary"]},
            "questions": {
                "sentiment": {
                    "type": "choice",
                    "instructions": "Classify the immediate stock price impact of this financial news.",
                    "criteria": CHOICE_CRITERIA,
                }
            },
        }
        ts = time.perf_counter()
        encoded = encode_record(processor.tokenizer, record, processor=processor)
        batch = collate_records([encoded], processor.tokenizer.pad_token_id, torch.device(device))
        with torch.inference_mode():
            logits = model(batch)[0]
        if device == "cuda":
            torch.cuda.synchronize()
        ms = (time.perf_counter() - ts) * 1000.0
        q_info = encoded.questions[0]
        probs = logits[0].float().softmax(-1).tolist()
        best_idx = max(range(len(probs)), key=lambda idx: probs[idx])
        pred = str(q_info.option_ids[best_idx]).upper()
        records.append({
            "lang": item["lang"],
            "gold": item["label"],
            "pred": pred if pred in ("BUY", "SELL", "HOLD") else "HOLD",
            "latency_ms": ms,
        })

    vram_mb = (torch.cuda.max_memory_allocated() / (1024 * 1024)) if device == "cuda" else 0.0
    del model, processor
    if device == "cuda":
        torch.cuda.empty_cache()
    return compute_metrics("clef-flash", backend, load_sec, vram_mb, records)


def benchmark_laya_router() -> BenchmarkMetrics:
    """Laya 언어 자동 라우터 벤치마크: 영문(US) -> laya-en, 다국어(KR) -> laya-multilingual."""
    import re
    import torch
    import laya

    device = "cuda" if torch.cuda.is_available() else "cpu"
    backend = f"laya-router:en+multilingual({device})"
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    t0 = time.perf_counter()
    agent_en = laya.load("convaiinnovations/laya", subfolder=None, device=device)
    agent_ml = laya.load("convaiinnovations/laya", subfolder="multilingual", device=device)
    load_sec = time.perf_counter() - t0

    questions = {
        "sentiment": {
            "type": "choice",
            "instructions": "Classify the immediate stock price impact of this financial news.",
            "criteria": CHOICE_CRITERIA,
        }
    }
    _ = agent_en.predict("Warmup.", questions)
    _ = agent_ml.predict("워밍업.", questions)
    if device == "cuda":
        torch.cuda.synchronize()

    records: List[Dict[str, Any]] = []
    for item in GOLDEN_DATASET:
        state = f"Symbol: {item['symbol']}\nTitle: {item['title']}\nSummary: {item['summary']}"
        has_kr = bool(re.search(r"[가-힣]", state))
        agent = agent_ml if has_kr else agent_en
        ts = time.perf_counter()
        out = agent.predict(state, questions)
        if device == "cuda":
            torch.cuda.synchronize()
        ms = (time.perf_counter() - ts) * 1000.0
        records.append({
            "lang": item["lang"],
            "gold": item["label"],
            "pred": _extract_laya_choice(out),
            "latency_ms": ms,
        })

    vram_mb = (torch.cuda.max_memory_allocated() / (1024 * 1024)) if device == "cuda" else 0.0
    del agent_en, agent_ml
    if device == "cuda":
        torch.cuda.empty_cache()
    return compute_metrics("laya-router", backend, load_sec, vram_mb, records)


def benchmark_hf_local(model_key: str) -> BenchmarkMetrics:
    """모델 키에 맞는 네이티브 System-1 어댑터로 디스패치한다."""
    k = model_key.strip().lower()
    if k == "laya-router":
        return benchmark_laya_router()
    if k in ("laya", "laya-en", "laya-multilingual"):
        return benchmark_laya("laya-multilingual" if k == "laya-multilingual" else "laya-en")
    if k == "von":
        return benchmark_von()
    if k in ("clef", "clef-flash"):
        return benchmark_clef_flash()
    return BenchmarkMetrics(
        model_name=model_key, backend="unknown", load_time_sec=0.0, vram_peak_mb=0.0,
        samples=0, p50_ms=0.0, p95_ms=0.0, overall_acc=0.0, kr_acc=0.0, us_acc=0.0,
        macro_f1=0.0, veto_recall_all=0.0, veto_precision_all=0.0,
        veto_recall_kr=0.0, veto_recall_us=0.0, composite_score=0.0,
        error=f"지원하지 않는 모델 키: {model_key}",
    )


def render_markdown_report(results: List[BenchmarkMetrics]) -> str:
    lines = [
        "# Open-Jev (System 1) A/B 벤치마크 리포트",
        "",
        f"- 평가셋: 총 {len(GOLDEN_DATASET)}건 (국장 한국어 20건 + 미장 영문 20건)",
        "- 종합 점수 가중치: `News Veto Recall 40% + Macro F1 35% + Latency(p95) 25%`",
        "",
        "| 모델 | 백엔드 | p50 (ms) | p95 (ms) | VRAM (MB) | 전체 정확도 | KR 정확도 | US 정확도 | Macro F1 | Veto Recall (KR/US) | 종합점수 | 비고 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    valid = [r for r in results if not r.error]
    for r in sorted(results, key=lambda x: x.composite_score, reverse=True):
        note = r.error or "OK"
        lines.append(
            f"| **{r.model_name}** | `{r.backend}` | {r.p50_ms:.1f} | {r.p95_ms:.1f} | {r.vram_peak_mb:.0f} "
            f"| {r.overall_acc:.1%} | {r.kr_acc:.1%} | {r.us_acc:.1%} | {r.macro_f1:.3f} "
            f"| {r.veto_recall_all:.1%} ({r.veto_recall_kr:.0%}/{r.veto_recall_us:.0%}) "
            f"| **{r.composite_score:.3f}** | {note[:60]} |"
        )
    if valid:
        winner = max(valid, key=lambda x: x.composite_score)
        lines.extend([
            "",
            f"## 선정 결과: **`{winner.model_name}`** (종합점수 `{winner.composite_score:.3f}`, p95 `{winner.p95_ms:.1f}ms`, Veto Recall `{winner.veto_recall_all:.1%}`)",
        ])
    return "\n".join(lines) + "\n"


async def async_main(args: argparse.Namespace) -> None:
    results: List[BenchmarkMetrics] = []
    if args.endpoints:
        for pair in args.endpoints.split(","):
            if "=" not in pair:
                continue
            name, url = pair.split("=", 1)
            results.append(await benchmark_http_endpoint(name.strip(), url.strip()))
    else:
        for m in [x.strip() for x in args.models.split(",") if x.strip()]:
            results.append(benchmark_hf_local(m))

    report_md = render_markdown_report(results)
    print(report_md)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "open_jev_ab_report.md").write_text(report_md, encoding="utf-8")
    (out_dir / "open_jev_ab_report.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"리포트 저장 완료: {out_dir / 'open_jev_ab_report.md'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Open-Jev (Laya vs Clef vs Von) A/B 벤치마크")
    parser.add_argument("--models", default="laya,von,clef-flash", help="HF 로컬 벤치마크 모델 목록(쉼표 구분)")
    parser.add_argument("--endpoints", default="", help="로컬 /v1/systemone 서버 목록 (예: laya=http://127.0.0.1:8011,von=http://127.0.0.1:8012)")
    parser.add_argument("--output-dir", default="data/benchmarks", help="벤치마크 결과 저장 디렉터리")
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
