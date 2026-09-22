# Finance Jarvis

한국투자증권(KIS) OpenAPI 기반 스윙트레이딩 자동매매 엔진. 30분봉 기반 기술적
시그널이 매매 방향/타이밍의 주 동력이고, 목표비중 밴드는 과대비중을 강제로
줄이는 보조 리스크 상한으로만 쓰인다. 여기에 뉴스 LLM 감성분석과 (선택) 펀더멘털
밸류에이션 시그널을 결합하고, 개별 포지션 손절/트레일링익절이 항상 최우선으로
평가된다. 국내주식·미국주식(NASD/NYSE/AMEX) 지원. 기본은 모의투자.

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
        ├─ core/exit_guard.py     — 손절/트레일링익절 (밴드·쿨다운과 무관하게 항상 최우선 평가)
        ├─ core/signal_engine.py  — 종목 1개당 정확히 하나의 평시 액션을 만드는 단일 결정 함수
        ├─ core/indicators.py     — RSI/MACD/볼린저밴드 (일봉 + 30분봉 양쪽에 재사용)
        ├─ core/intraday.py       — 30분봉 캐시/백필/리샘플 오케스트레이션
        ├─ core/news/             — 네이버 뉴스 수집 → Gemini 감성분석 → 시간가중 평균
        ├─ core/fundamentals/     — (선택) PER/PBR 백분위 + 목표주가 괴리 + 52주 위치 밸류에이션
        ├─ core/risk.py           — 쿨다운/일일손실한도/VI게이트/WS staleness/kill switch
        ├─ core/kis_domestic.py, core/kis_overseas.py — 국내/해외 KIS REST 래퍼
        └─ core/db.py             — SQLite(data/jarvis.db)에 전부 기록 (크래시 복구 근거)
```

### 매매 판단: 스윙 시그널이 방향을, 밴드는 상한만 정한다

`core/signal_engine.py`의 `decide()`가 사이클마다 종목당 한 번 호출되는 평시 매매의
유일한 결정 지점이다(손절/트레일링익절은 이보다 먼저, 별도 경로로 평가된다 —
아래 참고).

- **목표비중+밴드+버퍼(기본 5%+5%)를 넘어선 과대비중만** 시그널과 무관하게
  강제로 밴드 경계까지 축소한다(`band_ceiling_forced_trim`). 저비중은 리스크가
  아니므로 "강제 매수"는 없다 — 매수 여부/시점은 전적으로 스윙 시그널의 몫이다.
- **그 외의 모든 경우**엔 기술(일봉, 30%) + 30분봉 스윙(50%, 주 동력) + 뉴스감성
  (20%) + (선택) 밸류에이션(20%) 결합강도가 `SWING_SIGNAL_THRESHOLD`(기본 0.5)를
  넘으면 그 방향으로 매매하며, 강도에 비례해 자산의 최대 `SWING_TRADE_MAX_EQUITY_FRACTION`
  (기본 15%)까지 거래한다.
- 모든 주문은 최종적으로 `MAX_ORDER_NOTIONAL_KRW`(회당 최대 금액), `MAX_POSITION_PCT`
  (종목당 최대 비중)로 다시 한번 축소된다.

### 손절 / 트레일링 익절 (`core/exit_guard.py`)

개별 포지션의 평단가/고점 대비 하락률만 보는 순수 게이트로, 사이클마다 밴드·시그널
판단보다 먼저 평가된다. 발동하면 **쿨다운과 일일손실한도를 의도적으로 우회**한다
(손실을 줄이는 강제청산을 "새 재량매매 제한" 게이트가 막으면 안 되므로) — 단 VI
발동 중에는 그대로 보류된다(KIS가 VI 중엔 어차피 체결을 안 시켜준다).

- **손절**: 평단가 대비 `STOP_LOSS_PCT`(기본 -7%) 하락 시 전량 즉시매도. 국내는
  체결 확실성을 위해 시장가, 해외는 시장가 미지원이라 직전가 대비 2% 할인 지정가.
- **트레일링 익절**: 진입 후 한 번이라도 이익 구간에 도달했던 포지션이 고점 대비
  `TRAILING_TAKE_PROFIT_PCT`(기본 -5%) 하락하면 전량매도. 한 번도 이익을 본 적
  없는 포지션의 단순 하락은 손절 조건이 담당한다(트레일링과 손절이 같은 하락을
  이중으로 잡지 않도록 분리).
- 진입/청산 시점의 평단가·고점은 `positions` 테이블에 저장되며, 추가매수/부분매도
  중에는 리셋되지 않는다(추가매수한다고 트레일링 보호가 초기화되면 안 되므로).

### 30분봉 스윙 데이터 (`core/intraday.py`)

기술적 지표가 일봉만으로 계산되면 30분 사이클로 재평가해도 사실상 매 사이클 같은
값이 나온다(하루 변동이 지표에 거의 반영 안 됨) — 그래서 KIS 분봉 API를 새로
연동했다. 국내는 1분봉만 제공돼(`FHKST03010200`/`FHKST03010230`, 모의투자에서도
동작 확인됨) 09:00 KST 앵커로 30분봉으로 리샘플링하고, 해외는 `NMIN=30`으로 30분봉을
직접 받는다(`HHDFS76950200`). 처음 등록한 종목은 최소 30개 봉이 쌓일 때까지 과거
데이터를 백필하는데, 모의투자 초당 1건 제한 때문에 사이클당 최대
`INTRADAY_BACKFILL_SYMBOLS_PER_CYCLE`(기본 3) 종목만 백필하고 나머지는 다음
사이클로 미룬다 — 그동안은 HOLD로 안전하게 대기한다.

### 뉴스 감성분석 (Phase 2)

네이버 금융 뉴스(`core/news/naver_finance.py`)를 수집해 중복 헤드라인을 걸러내고,
새 기사만 Gemini(`GEMINI_MODEL`)로 BUY/SELL/HOLD + 강도(0~1)를 구조화 출력으로
받는다. 결과는 기사 URL 기준으로 캐시되고, 종목별 최종 감성은 12시간 반감기로
최신 뉴스에 가중치를 준 평균이다. LLM 실패/파싱 오류는 항상 HOLD로 안전하게
폴백한다. `GEMINI_API_KEY`가 없으면 이 파이프라인 전체가 꺼지고 기술적 지표만
쓰는 Phase 1 동작으로 자동 전환된다.

### 펀더멘털 밸류에이션 (선택, `ENABLE_FUNDAMENTAL_VALUATION`, 기본 꺼짐)

국내 종목 전용. DCF 같은 정밀 밸류에이션이 아니라 퍼센타일/괴리율 휴리스틱 3개를
가중합산한다: ① 자기 과거 PER/PBR 백분위(40%, KIS `finance-financial-ratio` +
`get_daily_chart` 종가로 계산), ② 네이버 컨센서스 목표주가 괴리율(35%,
`m.stock.naver.com` 비공식 API, ±30% 캡), ③ 52주 레인지 내 위치(25%). ROE/부채비율이
극단적이면 BUY 강도만 감쇠시키고 방향은 안 뒤집는다. 분기 단위로만 바뀌는 데이터라
`VALUATION_CACHE_TTL_HOURS`(기본 24시간) 동안 캐시해 30분 사이클마다 재계산하지
않는다. 기본은 꺼져 있다 — KIS `invest-opinion`/`finance-financial-ratio` 응답
형식을 스모크테스트로 직접 확인한 뒤(`core/fundamentals/valuation.py`) 켤 것을
권장한다. DART(전자공시) 연동은 별도 API 키가 필요해 Phase 2로 미뤘다
(`core/fundamentals/dart_client.py`는 스텁).

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
| 재주문 쿨다운 | 30분 | 동일 종목 재주문 최소 간격 (`ORDER_COOLDOWN_SEC`, 손절/익절은 우회) |
| 손절 | -7% | 평단가 대비 (`STOP_LOSS_PCT`) — 밴드/쿨다운 무관하게 즉시 전량매도 |
| 트레일링 익절 | -5% | 진입 후 고점 대비 (`TRAILING_TAKE_PROFIT_PCT`) |
| 밴드 상한(강제축소 트리거) | 목표비중+5%+5% | 넘으면 시그널과 무관하게 강제 축소 (`REBALANCE_BAND_PCT`+`BAND_CEILING_BUFFER_PCT`) |
| 스윙매매 1회 최대비중 | 자산의 15% | 시그널 강도에 비례 (`SWING_TRADE_MAX_EQUITY_FRACTION`) |
| 종목당 최대 비중 | 30% | 매수 후 비중이 이 이상이면 클램프 |
| 회당 최대 주문금액 | 100만원 | — |
| 일일 손실 한도 | 자산 대비 3% | 도달 시 당일 신규주문 차단(손절/익절 체결은 여기에 반영됨) |
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
| 4 | 스윙 전환: 30분봉 시그널 주동력화 + 손절/트레일링익절 + 펀더멘털 밸류에이션 | 신규 엔드포인트(분봉/투자의견/재무비율/네이버컨센서스) 모의투자로 라이브 검증, `decide()`/`exit_guard`/`intraday`/`fundamentals` 단위 점검 완료. **실제 엔진 루프를 통한 다중 사이클 라이브 관찰은 아직 안 함** — 실거래 전환 전 며칠 모의투자 관찰 권장 |

## 대시보드 사용법 (Streamlit, "1 KIS 자동매매" 페이지)

접속하면 사이드바에 모의/실전 뱃지와 엔진 실행 상태 뱃지가 항상 보인다.
자동 새로고침 토글로 5~60초 주기 갱신을 켤 수 있다.

- **📊 개요**: 엔진 상태(Heartbeat/체결통보 WS/Kill switch) 카드, ▶️ 시작 /
  ⏸️ 정지 / 🛑 긴급정지(미체결 전량취소) 버튼, 보유종목 장부가치 비중 파이차트.
- **⚙️ 설정 · 리스크**: 위 안전장치 표의 실제 현재 실행값을 읽기 전용 카드로 표시
  (`.env`로 값을 바꾸고 재시작하면 여기 숫자도 바뀐다) — VSCode에서 `config.py`를
  직접 열어볼 필요가 없어짐. 손절/트레일링익절 임계값, 스윙 시그널 임계강도/최대비중,
  밸류에이션 활성 여부도 여기서 확인한다.
- **💼 포트폴리오 · 비중**: 종목 등록(국내/해외), 목표비중 수동 설정, LLM 비중제안
  요청 및 승인/거부, 현재 포지션(수량/평균단가/장부가치) 테이블.
- **🧾 매매 로그**: 최근 의사결정 이력. 종목/액션(BUY·SELL·NO_OP)으로 필터링,
  현재비중/목표비중/드리프트/기술시그널/감성시그널을 한 줄로 확인. 손절/트레일링익절/
  스윙시그널/밴드상한축소는 사유 컬럼에 뱃지로 표시된다. 행을 선택하면 그 사이클의
  원시 스냅샷(가격, 30분봉 시그널, 감성/밸류에이션 근거, 손절 판정 등 `context_json`)을
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
core/exit_guard.py       손절/트레일링익절 판단 (순수 함수)
core/intraday.py         30분봉 캐시/백필/리샘플 오케스트레이션
core/signal_engine.py    평시 매매 단일 결정 함수 decide()
core/indicators.py       기술적 시그널 (pandas-ta, 일봉+30분봉 공용)
core/llm/                Gemini LLMProvider (provider-agnostic 인터페이스)
core/news/               네이버 뉴스 수집 + 감성분석
core/fundamentals/       (선택) PER/PBR 백분위 + 목표주가 괴리 밸류에이션
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
- 스윙 시그널 매매(밴드 안/상한 이하 매매)에 수수료·세금 대비 기대수익을 따지는
  로직이 여전히 없음 — 손절/트레일링익절로 큰 손실은 막지만, 신호가 자주 뒤집히면
  소액 왕복매매로 비용이 누적될 수 있다. 30분 윈도우로 완화했지만 근본 해결책은
  아님.
- `SWING_TRADE_MAX_EQUITY_FRACTION`(15%)은 기존(2%) 대비 큰 폭으로 올라간 값이라
  리스크 프로파일이 실질적으로 달라졌다 — 실거래 전환 전 모의투자로 실제 매매
  빈도/사이즈를 며칠 관찰할 것을 권장.
- 펀더멘털 밸류에이션의 KIS `invest-opinion`/`finance-financial-ratio` 응답
  필드는 모의투자로 직접 호출해 확인은 했지만(스모크테스트), 다양한 종목/기간에
  걸친 광범위한 검증은 아님. 네이버 컨센서스의 `recommMean`(증권사 등급) 방향성은
  1개 종목 표본만 확인해 스코어에는 안 쓰고 참고용으로만 저장한다.
- 국내 분봉은 KIS가 1분봉만 줘서(FHKST03010200/230) 30분봉으로 클라이언트에서
  리샘플링한다 — 09:00 KST 앵커 기준이며, 사이클 타이밍이 불규칙해도 절대 버킷
  기준으로 캐시하므로 안전하지만 첫 백필(종목당 약 56콜)은 모의투자 1req/sec
  제약 때문에 여러 사이클에 걸쳐 분산된다(`INTRADAY_BACKFILL_SYMBOLS_PER_CYCLE`).

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
- **분봉/투자의견/재무비율 조회(FHKST03010200/230, FHKST663300C0, FHKST66430300)는
  실전/모의 구분 없는 단일 tr_id**이며 모의투자로 직접 호출해 정상 동작을 확인했음.
- **네이버 컨센서스 API(`m.stock.naver.com/api/stock/{symbol}/integration`)는
  비공식 엔드포인트** — `core/news/naver_finance.py`가 쓰는 뉴스 API와 마찬가지로
  언제든 바뀔 수 있다. 실패해도 예외를 던지지 않고 밸류에이션 컴포넌트만 빠짐.
