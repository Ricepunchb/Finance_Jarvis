# Finance Jarvis

한국투자증권(KIS) OpenAPI 기반 자동매매/리밸런싱 엔진. 목표비중 대비 드리프트를
밴드로 관리하고, 밴드 안에서는 기술적 지표 + 뉴스 LLM 감성분석 시그널로 타이밍을
잡는다. 국내주식·미국주식(NASD/NYSE/AMEX) 지원. 기본은 모의투자.

`app.py`는 별개의 개인용 리서치 도구(yfinance 기반 종목 스캐너/뉴스 감성분석,
`my_portfolio.json` 워치리스트 사용)이며 KIS 자동매매와는 무관하다. KIS 자동매매는
Streamlit의 좌측 페이지 메뉴 중 **"1 KIS 자동매매"**에서 별도로 동작한다.

## 빠른 시작

1. `.env.template`을 참고해 `.env` 작성. `KIS_APP_KEY`/`KIS_APP_SECRET`/
   `KIS_ACCOUNT_NO`/`KIS_HTS_ID`(실시간 체결통보 구독용)는 필수, `GEMINI_API_KEY`는
   감성분석/비중제안(LLM) 기능을 쓸 때만 필요(없으면 자동으로 기술적 지표만 사용).
2. `uv sync`
3. 터미널 1 — 제어 플레인(FastAPI, 실제 엔진 로직이 도는 곳):
   ```
   uv run uvicorn api.main:app --port 8800
   ```
4. 터미널 2 — 대시보드(Streamlit, 제어 플레인을 폴링하는 thin client):
   ```
   uv run streamlit run app.py
   ```
   브라우저에서 열리면 좌측 메뉴 → **"1 KIS 자동매매"** 클릭.
5. 대시보드에서: 종목 등록 → 목표 비중 설정(수동 또는 LLM 제안 후 승인) → **▶️ 시작**.

**실전투자 전환 주의**: `.env`의 `IS_MOCK=False`만으로는 실거래가 시작되지 않는다.
`I_UNDERSTAND_REAL_MONEY_RISK=true`까지 명시적으로 같이 설정해야 하는 이중 안전장치가
걸려 있다. 처음 써보는 거라면 항상 모의투자(`IS_MOCK=True`, 기본값)로 충분히
검증할 것.

## 어떻게 작동하는가

### 아키텍처

```
Streamlit (pages/1_KIS_자동매매.py)   ← thin client, 엔진 로직 없음
        │  HTTP 폴링
        ▼
FastAPI 제어 플레인 (api/main.py, :8800)
        │  start/stop 시 백그라운드 asyncio 태스크로 기동
        ▼
TradingEngine (core/engine.py)  — 30분마다 한 사이클, 등록된 전 종목 순회
        │
        ├─ core/signal_engine.py  — 종목 1개당 정확히 하나의 액션을 만드는 단일 결정 함수
        ├─ core/indicators.py     — RSI/MACD/볼린저밴드 기반 기술적 시그널
        ├─ core/news/             — 네이버 뉴스 수집 → Gemini 감성분석 → 시간가중 평균
        ├─ core/risk.py           — 쿨다운/일일손실한도/VI게이트/WS staleness/kill switch
        ├─ core/kis_domestic.py, core/kis_overseas.py — 국내/해외 KIS REST 래퍼
        └─ core/db.py             — SQLite(data/jarvis.db)에 전부 기록 (크래시 복구 근거)
```

### 매매 판단: 밴드가 방향을, 시그널이 크기를 정한다

`core/signal_engine.py`의 `decide()`가 사이클마다 종목당 한 번 호출되는 유일한
결정 지점이다.

- **목표비중 ±5%(`REBALANCE_BAND_PCT`) 밖으로 벗어나면** 방향은 항상 밴드가
  정한다(과소비중→매수, 과대비중→매도). 시그널이 밴드 교정과 강하게(강도 ≥0.7)
  반대 방향이면 이번엔 절반만 교정하고 다음 사이클에 재평가한다.
- **밴드 안에 있을 때만** 시그널이 방향을 정할 수 있는데, 이때도 기술적 지표가
  먼저 방향을 제시해야 하고(감성 단독으로는 진입 불가), 결합강도(기술 50% +
  감성 50%)가 0.5를 넘어야 하며, 거래 크기는 총자산의 2%로 제한된다.
- 모든 주문은 최종적으로 `MAX_ORDER_NOTIONAL_KRW`(회당 최대 금액), `MAX_POSITION_PCT`
  (종목당 최대 비중)로 다시 한번 축소된다.

### 뉴스 감성분석 (Phase 2)

네이버 금융 뉴스(`core/news/naver_finance.py`)를 수집해 중복 헤드라인을 걸러내고,
새 기사만 Gemini(`GEMINI_MODEL`)로 BUY/SELL/HOLD + 강도(0~1)를 구조화 출력으로
받는다. 결과는 기사 URL 기준으로 캐시되고, 종목별 최종 감성은 12시간 반감기로
최신 뉴스에 가중치를 준 평균이다. LLM 실패/파싱 오류는 항상 HOLD로 안전하게
폴백한다. `GEMINI_API_KEY`가 없으면 이 파이프라인 전체가 꺼지고 기술적 지표만
쓰는 Phase 1 동작으로 자동 전환된다.

### 목표비중 제안 → 승인 워크플로 (Phase 2.5)

`target_weights`(모든 제안의 append-only 로그) → 사람 승인 → `active_target_weights`
(엔진이 실제로 읽는 유일한 테이블). LLM이 제안한 비중은 대시보드에서 승인하기
전까지 매매에 전혀 반영되지 않는다. 수동으로 직접 입력한 비중은 이미 사람의
명시적 행동이므로 제안과 동시에 자동 승인된다.

### 안전장치 (`core/risk.py`, `core/config.py`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| 모의투자 기본 | `IS_MOCK=True` | 실거래는 `I_UNDERSTAND_REAL_MONEY_RISK=true`까지 있어야 진입 |
| 모니터링/매매 윈도우 | 30분 | 종목당 사이클 주기 (`CYCLE_INTERVAL_SEC`) |
| 재주문 쿨다운 | 30분 | 동일 종목 재주문 최소 간격 (`ORDER_COOLDOWN_SEC`) |
| 리밸런스 밴드 | ±5% | 이 안에서만 시그널이 방향을 정함 |
| 종목당 최대 비중 | 30% | 매수 후 비중이 이 이상이면 클램프 |
| 회당 최대 주문금액 | 100만원 | — |
| 일일 손실 한도 | 자산 대비 3% | 도달 시 당일 신규주문 차단 |
| 체결통보 WS 무응답 허용 | 120초 | 초과 시 신규주문 차단 |
| Kill switch | 수동 해제만 | VI/거래정지 종목 35%↑ 또는 60초내 주문거부 3회↑ 시 자동 트립 |

킬스위치는 **자동으로 절대 풀리지 않는다** — 이상 상황 원인을 사람이 직접 확인한
뒤 대시보드에서 명시적으로 해제해야 한다.

### Phase 구성

| Phase | 내용 | 상태 |
|---|---|---|
| 0 / 1 | 국내주식 모의투자 코어 (밴드+기술시그널) | 실증 완료(크래시 복구 포함) |
| 2 | 뉴스 LLM 감성분석 결합 | 실증 완료 |
| 2.5 | 목표비중 제안/승인 워크플로 | 실증 완료 |
| 3 | 해외주식(미국) 지원 | 시세/잔고 조회까지 검증, **실주문 체결 전체 사이클은 미국 정규장 시간에 아직 미검증** |

## 대시보드 사용법 (Streamlit, "1 KIS 자동매매" 페이지)

접속하면 사이드바에 모의/실전 뱃지와 엔진 실행 상태 뱃지가 항상 보인다.
자동 새로고침 토글로 5~60초 주기 갱신을 켤 수 있다.

- **📊 개요**: 엔진 상태(Heartbeat/체결통보 WS/Kill switch) 카드, ▶️ 시작 /
  ⏸️ 정지 / 🛑 긴급정지(미체결 전량취소) 버튼, 보유종목 장부가치 비중 파이차트.
- **⚙️ 설정 · 리스크**: 위 안전장치 표의 실제 현재 실행값을 읽기 전용 카드로 표시
  (`.env`로 값을 바꾸고 재시작하면 여기 숫자도 바뀐다) — VSCode에서 `config.py`를
  직접 열어볼 필요가 없어짐.
- **💼 포트폴리오 · 비중**: 종목 등록(국내/해외), 목표비중 수동 설정, LLM 비중제안
  요청 및 승인/거부, 현재 포지션(수량/평균단가/장부가치) 테이블.
- **🧾 매매 로그**: 최근 의사결정 이력. 종목/액션(BUY·SELL·NO_OP)으로 필터링,
  현재비중/목표비중/드리프트/기술시그널/감성시그널을 한 줄로 확인. 행을 선택하면
  그 사이클의 원시 스냅샷(가격, 시그널 강도, 감성 근거 등 `context_json`)을
  펼쳐볼 수 있다.

## 리포 구조

```
core/kis_client.py       토큰 캐시 + rate limit 저수준 KIS REST 클라이언트
core/kis_common.py       KisApiError / ensure_ok (국내·해외 공용)
core/kis_domestic.py     국내 typed API 래퍼
core/kis_overseas.py     해외 typed API 래퍼
core/tr_ids.py           모든 tr_id 중앙관리
core/websocket_client.py 체결통보 웹소켓 (국내+해외 동시 구독)
core/db.py               SQLite 스키마 + 쿼리 (data/jarvis.db, git 추적 안 함)
core/risk.py             리스크 게이트/kill switch
core/rebalancer.py       밴드 드리프트 계산 (순수 함수)
core/signal_engine.py    단일 결정 함수 decide()
core/indicators.py       기술적 시그널 (pandas-ta)
core/llm/                Gemini LLMProvider (provider-agnostic 인터페이스)
core/news/               네이버 뉴스 수집 + 감성분석
core/reconciliation.py   크래시 복구 대조 로직
core/engine.py           오케스트레이터
api/main.py              FastAPI 제어 플레인 (:8800)
pages/1_KIS_자동매매.py  Streamlit 대시보드
```

## 알려진 한계

- 해외주식 실주문 체결 + reconciliation 전체 사이클이 미국 정규장 시간에
  아직 검증되지 않음 (Phase 3 마무리 작업).
- 국내/미국 모두 휴장일 캘린더가 없음 — 요일만으로 장 시간을 근사 판단.
- 미국장 서머타임 전환은 월 단위 근사치(정확한 DST 전환일 미반영).
- 전술매매(밴드 안 시그널 매매)에 수수료·세금 대비 기대수익을 따지는 로직이
  없어, 신호가 자주 뒤집히면 소액 왕복매매로 비용이 누적될 수 있음. 30분
  윈도우로 완화했지만 근본 해결책은 아님.

## 자주 만나는 KIS API 오류

- **토큰 발급 1분당 1회 제한**(모의투자) — 짧은 간격으로 재실행하면 `EGW00133`.
- **모의투자 REST는 초당 1건 제한** — `core/kis_client.py`가 `IS_MOCK`일 때
  자동으로 낮춰서 처리함.
- **국내 매도가능수량조회(`inquire-psbl-sell`)는 모의투자 미지원**(`EGW02006`)
  — 보유수량으로 폴백하는 로직이 이미 들어가 있음.
- **모의투자 주문은 계좌가 "모의투자 주문서비스"에 등록되어 있어야 함** —
  안 되어 있으면 `IGW00002`(계좌불일치) 또는 `40910000`(모의투자 주문불가).
- **Gemini 모델명은 자주 deprecate됨** — 404가 뜨면 에러 메시지가 알려주는
  대체 모델명으로 `GEMINI_MODEL`을 갱신할 것.
