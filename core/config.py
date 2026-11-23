# core/config.py
import os
from pathlib import Path
from typing import Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent


def get_active_profile_name() -> str:
    """활성 프로필 이름을 반환. JARVIS_PROFILE 환경변수 우선, 그 다음 .active_profile 파일."""
    env_prof = os.getenv("JARVIS_PROFILE")
    if env_prof:
        return env_prof.strip()
    active_file = _ROOT / ".active_profile"
    if active_file.exists():
        try:
            val = active_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass
    return "default"


_ACTIVE_PROFILE = get_active_profile_name()
_PROFILE_ENV_PATH = _ROOT / f".env.{_ACTIVE_PROFILE}"
_TARGET_ENV_FILE = f".env.{_ACTIVE_PROFILE}" if _PROFILE_ENV_PATH.exists() else ".env"

_DEFAULT_PROFILE_PORTS = {
    "isa": (8800, 8501),
    "real": (8801, 8502),
    "mock": (8802, 8503),
    "meritz": (8803, 8504),
}
_DEFAULT_PROFILE_NAMES = {
    "isa": "ISA 절세 계좌",
    "real": "실전 일반 계좌 (수수료 무료)",
    "mock": "모의투자 계좌",
    "meritz": "메리츠 해외 실전 계좌",
    "default": "기본 계좌",
}


class Settings(BaseSettings):
    PROFILE_NAME: str = _ACTIVE_PROFILE
    PROFILE_DISPLAY_NAME: str = _DEFAULT_PROFILE_NAMES.get(_ACTIVE_PROFILE, "Finance Jarvis")
    API_PORT: int = _DEFAULT_PROFILE_PORTS.get(_ACTIVE_PROFILE, (8800, 8501))[0]
    FRONT_PORT: int = _DEFAULT_PROFILE_PORTS.get(_ACTIVE_PROFILE, (8800, 8501))[1]

    # 브로커 구분: "kis"(한국투자증권 - 국내 전용) | "meritz"(메리츠증권 - 해외 전용)
    BROKER: str = "meritz" if _ACTIVE_PROFILE == "meritz" else "kis"

    KIS_APP_KEY: str = ""
    KIS_APP_SECRET: str = ""
    KIS_ACCOUNT_NO: str = ""    # 예: 12345678-01
    KIS_HTS_ID: str = ""        # 실시간 체결통보(웹소켓) 구독에 필요한 HTS 로그인 ID
    IS_MOCK: bool = True        # 기본값은 모의투자

    # --- 메리츠증권 Open API (해외 계좌 전용) ---
    MERITZ_APP_KEY: str = ""
    MERITZ_APP_SECRET: str = ""
    MERITZ_BASE_URL: str = "https://openapi.imeritz.com:9443"
    MERITZ_WS_URL: str = "wss://openapi.imeritz.com:29443/websocket"
    # 메리츠는 모의투자 서버가 없으므로 SHADOW_MODE=True(드라이런)로 시그널/주문 판단만 기록하고 실제 주문 전송을 차단할 수 있다.
    SHADOW_MODE: bool = False
    MAX_ORDER_NOTIONAL_USD: float = 500.0  # 해외 1회 주문 최대 금액(USD 하드캡)

    # True면 실시간 체결통보를 위한 웹소켓 클라이언트를 구동한다.
    # 동일 HTS ID를 사용하는 복수 계좌(실전 일반 + ISA) 동시 구동 시 단일 세션 충돌 방지를 위해 보조 계좌는 False 권장.
    ENABLE_WEBSOCKET: bool = True

    # True면 국내(KRX 주식·ETF)만 매매·조회·발굴·등록한다. 해외는 수수료 부담이 커서 기본 비활성.
    # DB에 이미 있는 해외 종목/체결 이력은 지우지 않고 엔진이 건너뛸 뿐이다(분석 대시보드에서는 그대로 보인다).
    DOMESTIC_ONLY: bool = True
    # 발굴 후보에서 뺄 ETF 이름 키워드(쉼표 구분, 대소문자 무시). 레버리지·인버스·파생 기반은 스윙 신호가
    # 성과를 설명하지 못하고 변동성 끌림이 커서, 머니마켓·CD금리 같은 현금성은 가격이 거의 안 움직여서 제외한다.
    ETF_EXCLUDE_NAME_KEYWORDS: str = "레버리지,인버스,곱버스,선물,커버드콜,2X,3X,머니마켓,CD금리,KOFR,단기채,단기통안"

    # 실전투자(IS_MOCK=False)로 전환하려면 이 값도 명시적으로 true여야 한다.
    # 실수로 .env의 IS_MOCK만 바꿔서 실거래가 시작되는 사고를 막기 위한 이중 안전장치.
    I_UNDERSTAND_REAL_MONEY_RISK: bool = False

    # --- 리스크 관리 기본값 (모두 .env로 조정 가능) ---
    REBALANCE_BAND_PCT: float = 0.05       # 목표비중 대비 허용 드리프트 (±5%)
    MAX_POSITION_PCT: float = 0.30         # 종목당 총자산 대비 최대 비중 하드캡
    MAX_ORDER_NOTIONAL_KRW: int = 1_000_000  # 1회 주문 최대 금액
    MAX_DAILY_LOSS_PCT: float = 0.03       # 일일 손실 한도 (총자산 대비)
    ORDER_COOLDOWN_SEC: int = 1800         # 동일 종목 재주문 최소 간격 (모니터링 사이클과 동일한 30분 윈도우)
    WS_STALENESS_THRESHOLD_SEC: int = 120  # 체결통보 웹소켓 무응답 허용 시간

    # --- 손절/트레일링익절 ---
    STOP_LOSS_PCT: float = 0.06             # 평단가 대비 -6% 시 전량 강제매도 (최적화 그리드 서치 검증값)
    STOP_LOSS_COOLDOWN_DAYS: int = 5        # 손절 후 재진입 쿨다운 (달력일 기준, 역추세 연속 손절 방지)
    TRAILING_TAKE_PROFIT_PCT: float = 0.05  # 진입 후 고점 대비 -5% 하락 시 전량매도 (ATR 산출 불가 시 폴백)
    # ATR 기반 트레일링: 종목 변동성에 맞춰 폭/무장 조건을 정한다 (ATR% = ATR(일봉) / 현재가)
    ATR_LENGTH: int = 14
    TRAILING_ATR_MULT: float = 2.5               # 트레일링 폭 = 이 배수 x ATR%
    TRAILING_MIN_PCT: float = 0.03               # 폭 하한 (저변동 종목이 노이즈에 털리지 않게)
    TRAILING_MAX_PCT: float = 0.12               # 폭 상한 (고변동 종목이 너무 늦게 청산되지 않게)
    TRAILING_ARM_ATR_MULT: float = 1.5           # 고점이 평단 대비 이 배수 x ATR% 이상 올랐을 때만 트레일링 무장
    TRAILING_ARM_FALLBACK_PCT: float = 0.03      # ATR 산출 불가 시 무장 기준 (평단 대비 +3%)

    # --- 분할 온보딩 매수 (신규 편입/저비중 종목을 시그널 문턱 없이 며칠에 걸쳐 목표비중까지 채움) ---
    ONBOARDING_ENABLED: bool = True
    ONBOARDING_DAYS: int = 3                     # 목표금액을 이 일수로 나눈 만큼을 하루 매수 예산으로 사용 (최적화값)
    REQUIRE_UPTREND_FOR_ONBOARDING: bool = True  # 온보딩 매수 시 중기 상승 추세(SMA20 >= SMA60) 필수 (역추세 물타기 방지)
    REQUIRE_SMA20_FOR_BUY: bool = True           # 모든 신규 매수 시 단기 이평선(Close >= SMA20) 상회 필수 (칼날잡기 방지)
    ACCUMULATION_MODE: bool = False              # True 시 적립식/자산배분 모드 (단기 30분봉 SELL 신호로 코어 자산 바닥 털림 방지)

    # --- 포트폴리오 종목 삭제(정리 대기): 신규매수 중단 + 매도 신호 때 전량 매도, 기한 내 신호가 없으면 강제 청산 ---
    WINDDOWN_MAX_DAYS: int = 5                   # 삭제 후 이 일수(달력일)가 지나면 신호와 무관하게 전량 청산

    # --- 스윙 시그널 (밴드는 보조 리스크 상한으로 전환) ---
    INTRADAY_BAR_MINUTES: int = 30                  # CYCLE_INTERVAL_SEC(엔진 사이클)와 반드시 일치
    INTRADAY_LOOKBACK_CALENDAR_DAYS: int = 20        # 30분봉 백필 기간 (약 14거래일치)
    INTRADAY_BACKFILL_SYMBOLS_PER_CYCLE: int = 3     # 모의투자 1req/sec 제약 때문에 백필을 사이클에 분산

    # --- 신호 결합 가중치 (뉴스는 방향성 합산에서 제외, 악재 Veto 전용) ---
    SIGNAL_WEIGHT_TECH: float = 0.25                 # 일봉 중기 추세 확인
    SIGNAL_WEIGHT_INTRADAY: float = 0.45             # 30분봉 단기 타이밍 확인
    SIGNAL_WEIGHT_FLOW: float = 0.15                 # 고래/수급 선행 시그널 (US 4.3Y + KRX 3.8Y IS/OOS 검증값)
    SIGNAL_WEIGHT_VALUATION: float = 0.15            # 펀더멘털 밸류에이션 (활성화 시)

    # --- 수급 확인/역행 및 악재 Veto 파라미터 ---
    FLOW_CONFIRM_BOOST: float = 1.00                 # 수급과 기술 지표 방향 일치 시 강도 배수 (백테스트 MDD 검증값: 1.00)
    FLOW_DIVERGENCE_VETO_STRENGTH: float = 0.60      # 수급이 강한 SELL(>=0.60)이면 신규 BUY 억제
    NEWS_VETO_ENABLED: bool = True                   # 악재 뉴스 시 신규 매수만 차단하는 비대칭 필터
    NEWS_VETO_STRENGTH_THRESHOLD: float = 0.70       # 뉴스 감성 SELL & strength >= 0.70 시 BUY 차단

    # --- Layer A: 가격/거래량 수급 프록시 (국내·해외 공통, 백테스트 가능) ---
    FLOW_RVOL_WINDOW: int = 20                       # 상대거래량 기준 이동평균 봉 수
    FLOW_RVOL_SPIKE_THRESHOLD: float = 1.8           # RVOL >= 1.8 시 고래 거래량 스파이크 인정
    FLOW_CMF_WINDOW: int = 20                        # Chaikin Money Flow 윈도우

    # --- Layer B: 국내 KIS 수급 REST & 웹소켓 분 단위 급등락 탐지 ---
    ENABLE_KIS_FLOW_API: bool = True                 # 국내 프로그램/외인·기관 REST 조회 활성화
    ENABLE_WS_MARKET_STREAM: bool = True             # ENABLE_WEBSOCKET=True일 때 H0STCNT0/H0UPPGM0 구독
    WS_PROGRAM_TR_ID: str = "H0UPPGM0"               # 실시간 프로그램매매 TR (환경에 따라 오버라이드 가능)
    WS_MAX_SUBSCRIBED_SYMBOLS: int = 15              # H0STCNT0 + H0UPPGM0 동시 구독 시 최대 종목 수 (41건 한도 안전선)
    WS_MINUTE_SURGE_PCT: float = 0.015               # 1~3분 내 +1.5% 이상 급등 + 거래량 급증 시 매수 평가 트리거
    WS_MINUTE_PLUNGE_PCT: float = -0.015             # 1~3분 내 -1.5% 이상 급락 시 exit_guard 즉시 재평가
    WS_MINUTE_RVOL_MULT: float = 3.0                 # 직전 20분 평균 대비 3배 이상 체결량 급증
    WS_MINUTE_CTTR_BULL: float = 120.0               # 체결강도 >= 120% (매수 우위)
    WS_MINUTE_CTTR_BEAR: float = 80.0                # 체결강도 <= 80% (매도 우위)
    WS_FASTPATH_DEBOUNCE_SEC: int = 180              # 동일 종목 Fast-Path 재평가 최소 간격 (3분)
    WS_FASTPATH_SURGE_DRY_RUN: bool = True           # True면 급등 Fast-Path는 관측 로그만 기록 (급락 exit_guard는 항상 활성)

    # --- 시장 전체 레짐 필터 (급락장/사이드카 방어) ---
    MARKET_REGIME_ENABLED: bool = True
    MARKET_REGIME_CACHE_TTL_SEC: int = 300           # 5분 캐시
    MARKET_REGIME_INTRADAY_DROP_PCT: float = -0.015  # 대표 벤치마크(KODEX200/SPY) 당일 -1.5% 이하 시 RISK_OFF (IS/OOS 검증값)
    MARKET_REGIME_DOMESTIC_BENCHMARK: str = "069500" # KODEX 200
    MARKET_REGIME_OVERSEAS_BENCHMARK: str = "SPY"    # SPDR S&P 500 ETF
    MARKET_REGIME_DEFENSIVE_KEYWORDS: str = "국고채,종합채권,미국채,금현물,골드,SGOV,GLD,TLT,IEF,SHY"

    # --- 펀더멘털 밸류에이션 (기본 꺼짐 — 스모크테스트 후 켜는 것을 권장) ---
    ENABLE_FUNDAMENTAL_VALUATION: bool = False
    VALUATION_CACHE_TTL_HOURS: int = 24
    DART_API_KEY: str = ""  # opendart.fss.or.kr 무료 가입 후 발급 (Phase 2, 현재 미사용)

    DB_PATH: str = f"data/jarvis_{_ACTIVE_PROFILE}.db" if _ACTIVE_PROFILE != "default" else "data/jarvis.db"
    ENGINE_LOCK_PATH: str = f"data/engine_{_ACTIVE_PROFILE}.lock" if _ACTIVE_PROFILE != "default" else "data/engine.lock"

    # --- 공유 인텔리전스 계층 (마스터 데몬 & 공유 DB) ---
    INTEL_DB_PATH: str = "data/jarvis_intel.db"
    INTEL_LOCK_PATH: str = "data/intel_master.lock"
    # "master": 수집·중복제거·LLM 분석 전담 데몬 | "consumer": 공유 DB 읽기 참조 전용 계좌 엔진
    INTEL_ROLE: str = "consumer"
    # consumer 엔진에서 마스터 DB에 없는 종목 뉴스를 만났을 때 직접 분석 폴백 허용 여부
    INTEL_ALLOW_ON_DEMAND_FALLBACK: bool = False

    # --- LLM (뉴스 감성분석 / 목표비중 제안 / 3티어 라우팅) ---
    # provider-agnostic 설계: 이 값만 바꾸면 코드 변경 없이 다른 제공자로 교체 가능해야 한다.
    LLM_PROVIDER: str = "gemini"
    GEMINI_API_KEY: str = ""
    # 레거시 호환용 기본 모델 (명시적으로 FAST/SMART를 지정하지 않은 경우 폴백)
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    GEMINI_FALLBACK_MODEL: str = "gemini-3.6-flash"
    # Tier 2 (Fast): 뉴스 요약/번역, 증권사 리포트 배치, 인사이트 다이제스트, ETF 분류
    GEMINI_FAST_MODEL: str = "gemini-3.5-flash-lite"
    GEMINI_FAST_FALLBACK_MODEL: str = "gemini-3.6-flash"
    # Tier 3 (Smart): 포트폴리오 리밸런싱 등 고지능 추론 태스크 전용
    GEMINI_SMART_MODEL: str = "gemini-4-hydrogen"
    GEMINI_SMART_FALLBACK_MODEL: str = "gemini-3.6-flash"

    # --- LLM 예산 서킷브레이커 (기본: 주간 $1.50 ≈ ₩2,000) ---
    LLM_WEEKLY_COST_USD_LIMIT: float = 1.50
    LLM_DAILY_COST_USD_LIMIT: Optional[float] = None
    LLM_DAILY_TOKEN_LIMIT: Optional[int] = None

    # --- Tier 1: 로컬 Open-Jev (System 1) 의사결정 모델 ---
    # "off": 미사용 | "shadow": Gemini와 병행 로깅 | "active": News Veto / 1차 필터 실전 적용
    OPEN_JEV_MODE: str = "off"
    OPEN_JEV_MODEL: str = "laya-router"  # A/B 벤치마크 승자: "laya-router" (KR:multilingual + US:en) | "laya-multilingual" | "von"
    OPEN_JEV_ENDPOINT: str = ""   # 선택적 로컬 /v1/systemone 서버 URL (비어 있으면 Laya 인프로세스 추론)

    NEWS_LOOKBACK_HOURS: int = 24
    NEWS_MAX_ARTICLES_PER_SYMBOL: int = 5

    # --- AI 포트폴리오 에이전트 (Phase 5.0: 재비중 제안 검증 한도) ---
    # 종목당 상한은 기존 MAX_POSITION_PCT를 그대로 재사용한다 (중복 상수 금지).
    AI_REBALANCE_MAX_TURNOVER_PCT: float = 0.20        # 1회 제안의 총 회전율 상한 (sum|Δweight|/2)
    AI_REBALANCE_MAX_WEIGHT_DELTA_PCT: float = 0.10    # 종목당 1회 최대 비중 변화
    AI_REBALANCE_MAX_SYMBOLS_ADDED: int = 2
    AI_REBALANCE_MAX_SYMBOLS_REMOVED: int = 2
    AI_REBALANCE_MIN_SYMBOL_WEIGHT_PCT: float = 0.03
    AI_REBALANCE_MAX_PORTFOLIO_SYMBOLS: int = 20
    AI_REBALANCE_MIN_POSITION_VALUE_KRW: int = 250_000 # 종목당 최소 목표 배분 금액(원) — 자금 규모 대비 종목 과다 분산 방지
    AI_REBALANCE_MIN_PORTFOLIO_SYMBOLS_FLOOR: int = 4  # 소액 계좌에서도 최소 분산이 가능한 하한 종목 수
    AI_REBALANCE_REMOVED_COOLDOWN_DAYS: int = 7        # 삭제한 종목을 신규 편입 후보에서 제외하는 기본 기간 (고득점 발굴 시 즉시 면제)

    # --- AI 포트폴리오 에이전트 (Phase 5.1: 종목 발굴) ---
    DISCOVERY_TOP_N: int = 15  # candidate_universe 중 스크리닝 상위 몇 개까지 LLM에 보여줄지
    DISCOVERY_MIN_SCORE: float = 0.40  # 복합점수가 이 값 미만인 후보는 숏리스트/오늘의 발굴에서 제외 (상위 ~10% 고확신 복합 팩터 후보 진입)

    # --- 동적 종목 발굴 (전종목 마스터 + 모멘텀/증권사/뉴스/저평가/테마후발 소스) ---
    DISCOVERY_DYNAMIC_ENABLED: bool = True
    DISCOVERY_REFRESH_HOUR_KST: int = 16          # 장 마감 후 이 시각 이후 하루 1회 갱신
    DISCOVERY_DYNAMIC_TTL_DAYS: int = 5           # 소스에서 다시 안 잡히면 이 기간 뒤 후보에서 만료
    DISCOVERY_MAX_DYNAMIC: int = 60               # 동적 후보 최대 수 (모의투자 1req/sec에서 점수계산 시간 제한용)
    DISCOVERY_MIN_MARKET_CAP_EOK: int = 1000      # 시가총액 하한(억원) - 초소형 잡주 배제
    DISCOVERY_BROKER_LOOKBACK_DAYS: int = 14
    DISCOVERY_VALUE_POOL_SIZE: int = 300          # 저평가 소스가 네이버 컨센서스를 조회할 시총 상위 풀 크기
    DISCOVERY_VALUE_MIN_TARGET_GAP: float = 0.20  # 컨센서스 목표가 대비 최소 상승여력
    DISCOVERY_SHORTLIST_TTL_HOURS: int = 24
    # 복합점수 가중치 (tech=기술적 BUY 강도, value=밸류에이션 BUY 강도, early=하입 조기신호, broker=증권사 매수 추천)
    DISCOVERY_WEIGHTS: dict[str, float] = {"tech": 0.30, "value": 0.30, "early": 0.25, "broker": 0.15}
    # 관점별 최소 할당 - 한 관점이 숏리스트를 독식하지 않게 먼저 채운 뒤 나머지는 점수순
    DISCOVERY_ANGLE_QUOTA: dict[str, int] = {"value": 3, "early": 3, "momentum": 3, "broker": 2}

    # --- AI 리포트/뉴스 인사이트 묶음 및 정기 스케줄링 (인사이트 탭) ---
    INSIGHT_ENABLED: bool = True
    INSIGHT_SCHEDULE_HOURS_KST: list[int] = [9, 12, 15, 21]  # 아침 9시, 오후 12시, 오후 3시, 오후 9시 KST 정기 실행
    INSIGHT_RETENTION_DAYS: int = 365             # 1년 단위 보관 후 자동 정리
    INSIGHT_REPORT_ANALYZE_MAX_PER_RUN: int = 60  # 1회 갱신에서 본문 수집·분석할 최대 리포트 수 (백로그 상한)
    INSIGHT_REPORT_LLM_BATCH_SIZE: int = 8        # LLM 1회 호출당 리포트 수
    INSIGHT_HEADLINE_STORE_MAX: int = 300         # 표본 헤드라인 중 종목 매칭된 것 저장 상한
    INSIGHT_DIGEST_ENABLED: bool = True           # 묶음당 종합 AI 다이제스트 생성 여부
    # --- 성과/리스크 지표 (샤프/소티노 계산용) ---
    RISK_FREE_RATE_ANNUAL: float = 0.035  # 한국 무위험수익률 근사치 (연 기준, 필요시 조정)

    # --- 시장 심리 지표 (CNN Fear & Greed Index - AI 리밸런싱 LLM의 참고 맥락으로만 사용) ---
    FEAR_GREED_CACHE_TTL_HOURS: int = 3

    # --- AI 포트폴리오 에이전트 (Phase 5.2: 스케줄러 트리거) ---
    AI_REBALANCE_MIN_INTERVAL_SEC: int = 86400            # 에이전트 자체 의사결정 쿨다운 (주문 쿨다운과 별개)
    AI_REBALANCE_PERIODIC_INTERVAL_DAYS: int = 7          # 정기 재검토 주기 (주간 단위 하트비트)
    AI_REBALANCE_DRIFT_TRIGGER_BUFFER_PCT: float = 0.05   # REBALANCE_BAND_PCT를 넘어 이만큼 더 벗어나야 트리거
    AI_REBALANCE_NEWS_TRIGGER_STRENGTH: float = 0.7       # 보유종목 뉴스감성 강도가 이 이상이면 트리거
    AI_REBALANCE_DISCOVERY_TRIGGER_SCORE: float = 0.45    # 미보유 고득점 발굴 종목 출현 시 즉시 리밸런싱 트리거 임계치
    AI_REBALANCE_AUTO_APPLY: bool = False                 # True면 가드레일을 통과한 제안을 사람 승인 대기 없이 자동 반영

    # --- 백테스팅 기본값 ---
    BACKTEST_DEFAULT_BENCHMARK: str = "360750"            # TIGER 미국S&P500 (국내상장 S&P 500 ETF, KRW 기준)
    BACKTEST_DEFAULT_FEE_PCT: float = 0.00015             # 편도 수수료 0.015%
    BACKTEST_DEFAULT_TAX_PCT: float = 0.0018              # 매도 시 거래세 0.18%
    BACKTEST_DEFAULT_SLIPPAGE_PCT: float = 0.0005         # 체결 슬리피지 0.05%

    @property
    def kis_domain(self) -> str:
        # 모의투자 및 실전투자 도메인 분리
        if self.IS_MOCK:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def cano(self) -> str:
        """종합계좌번호 (계좌번호 앞 8자리)"""
        if not self.KIS_ACCOUNT_NO or "-" not in self.KIS_ACCOUNT_NO:
            return self.KIS_ACCOUNT_NO
        return self.KIS_ACCOUNT_NO.split("-")[0]

    @property
    def acnt_prdt_cd(self) -> str:
        """계좌상품코드 (계좌번호 뒤 2자리)"""
        if not self.KIS_ACCOUNT_NO or "-" not in self.KIS_ACCOUNT_NO:
            return "01"
        return self.KIS_ACCOUNT_NO.split("-")[1]

    def assert_trading_allowed(self) -> None:
        """실전투자 진입에 필요한 이중 확인을 강제한다. 엔진 시작 시 항상 호출해야 한다."""
        if self.BROKER.lower() == "meritz":
            if not self.SHADOW_MODE and not self.I_UNDERSTAND_REAL_MONEY_RISK:
                raise RuntimeError(
                    "메리츠 실전주문(SHADOW_MODE=False) 상태이지만 I_UNDERSTAND_REAL_MONEY_RISK=true가 "
                    "설정되지 않았습니다. 실거래를 의도한 것이 맞다면 .env.meritz에 명시적으로 추가하세요."
                )
            return
        if not self.IS_MOCK and not self.I_UNDERSTAND_REAL_MONEY_RISK:
            raise RuntimeError(
                "실전투자(IS_MOCK=False) 상태이지만 I_UNDERSTAND_REAL_MONEY_RISK=true가 "
                "설정되지 않았습니다. 실거래를 의도한 것이 맞다면 .env에 명시적으로 추가하세요."
            )

    # --- 해외 원주 발굴 ➔ 국내 대체 ETF 대리 매매 (Proxy Trading) [Deprecated] ---
    # 국장(국내 계좌)에서는 미장 종목을 직접이든 간접(프록시 ETF)이든 다루지 않도록 기본 비활성화.
    OVERSEAS_PROXY_TRADING_ENABLED: bool = False

    # --- 텔레그램 모바일 푸시 알림 ---
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_MIN_ALERT_STRENGTH: float = 0.60  # 60% 이상 강한 시그널일 때만 알림 발송 (손절/익절은 예외로 즉시 발송)

    @model_validator(mode="after")
    def _sync_profile_paths(self):
        if self.PROFILE_NAME and self.PROFILE_NAME != "default":
            # DB_PATH가 하드코딩 기본값이면 프로필별 DB로 자동 재할당
            if self.DB_PATH == "data/jarvis.db":
                self.DB_PATH = f"data/jarvis_{self.PROFILE_NAME}.db"
            if self.ENGINE_LOCK_PATH == "data/engine.lock":
                self.ENGINE_LOCK_PATH = f"data/engine_{self.PROFILE_NAME}.lock"
            if not self.PROFILE_DISPLAY_NAME:
                self.PROFILE_DISPLAY_NAME = _DEFAULT_PROFILE_NAMES.get(
                    self.PROFILE_NAME, f"프로필: {self.PROFILE_NAME}"
                )
        return self

    model_config = SettingsConfigDict(env_file=_TARGET_ENV_FILE, env_file_encoding="utf-8", extra="ignore")


settings = Settings()


async def get_effective_domestic_only(conn) -> bool:
    """DB state('domestic_only')가 설정되어 있으면 그 값을 우선하고, 없으면 settings.DOMESTIC_ONLY를 사용한다."""
    from core import db
    val = await db.get_state(conn, "domestic_only")
    if val is not None:
        return val == "1"
    return settings.DOMESTIC_ONLY


async def get_effective_proxy_trading(conn) -> bool:
    """DB state('proxy_domestic_trading')가 설정되어 있으면 그 값을 우선하고, 없으면 settings.OVERSEAS_PROXY_TRADING_ENABLED를 사용한다."""
    from core import db
    val = await db.get_state(conn, "proxy_domestic_trading")
    if val is not None:
        return val == "1"
    return settings.OVERSEAS_PROXY_TRADING_ENABLED


async def set_effective_proxy_trading(conn, enabled: bool) -> None:
    """DB state에 해외 종목 프록시 매매 활성화 여부를 저장한다."""
    from core import db
    await db.set_state(conn, "proxy_domestic_trading", "1" if enabled else "0")

