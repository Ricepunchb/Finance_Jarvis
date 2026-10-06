# core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    KIS_APP_KEY: str
    KIS_APP_SECRET: str
    KIS_ACCOUNT_NO: str    # 예: 12345678-01
    KIS_HTS_ID: str        # 실시간 체결통보(웹소켓) 구독에 필요한 HTS 로그인 ID
    IS_MOCK: bool = True   # 기본값은 모의투자

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

    # --- 포트폴리오 종목 삭제(정리 대기): 신규매수 중단 + 매도 신호 때 전량 매도, 기한 내 신호가 없으면 강제 청산 ---
    WINDDOWN_MAX_DAYS: int = 5                   # 삭제 후 이 일수(달력일)가 지나면 신호와 무관하게 전량 청산

    # --- 스윙 시그널 (밴드는 보조 리스크 상한으로 전환) ---
    INTRADAY_BAR_MINUTES: int = 30                  # CYCLE_INTERVAL_SEC(엔진 사이클)와 반드시 일치
    INTRADAY_LOOKBACK_CALENDAR_DAYS: int = 20        # 30분봉 백필 기간 (약 14거래일치)
    INTRADAY_BACKFILL_SYMBOLS_PER_CYCLE: int = 3     # 모의투자 1req/sec 제약 때문에 백필을 사이클에 분산

    # --- 펀더멘털 밸류에이션 (기본 꺼짐 — 스모크테스트 후 켜는 것을 권장) ---
    ENABLE_FUNDAMENTAL_VALUATION: bool = False
    VALUATION_CACHE_TTL_HOURS: int = 24
    DART_API_KEY: str = ""  # opendart.fss.or.kr 무료 가입 후 발급 (Phase 2, 현재 미사용)

    DB_PATH: str = "data/jarvis.db"
    ENGINE_LOCK_PATH: str = "data/engine.lock"

    # --- LLM (뉴스 감성분석 / 목표비중 제안) ---
    # provider-agnostic 설계: 이 값만 바꾸면 코드 변경 없이 다른 제공자로 교체 가능해야 한다.
    LLM_PROVIDER: str = "gemini"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.6-flash"
    # 주 모델이 5xx(예: 503 UNAVAILABLE - 수요 폭주)를 반환하면 이 모델로 한 번 더 시도한다.
    # 빈 문자열이면 폴백 없이 주 모델 결과만 사용.
    GEMINI_FALLBACK_MODEL: str = "gemini-2.5-flash"
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
    AI_REBALANCE_REMOVED_COOLDOWN_DAYS: int = 30       # 삭제한 종목을 신규 편입 후보에서 제외하는 기간

    # --- AI 포트폴리오 에이전트 (Phase 5.1: 종목 발굴) ---
    DISCOVERY_TOP_N: int = 15  # candidate_universe 중 스크리닝 상위 몇 개까지 LLM에 보여줄지
    DISCOVERY_MIN_SCORE: float = 0.6  # 복합점수가 이 값 미만인 후보는 숏리스트/오늘의 발굴에서 제외

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

    # --- AI 리포트/뉴스 인사이트 묶음 (인사이트 탭) ---
    INSIGHT_ENABLED: bool = True
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
    AI_REBALANCE_PERIODIC_INTERVAL_DAYS: int = 30         # 정기 재검토 주기
    AI_REBALANCE_DRIFT_TRIGGER_BUFFER_PCT: float = 0.05   # REBALANCE_BAND_PCT를 넘어 이만큼 더 벗어나야 트리거
    AI_REBALANCE_NEWS_TRIGGER_STRENGTH: float = 0.7       # 보유종목 뉴스감성 강도가 이 이상이면 트리거
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
        return self.KIS_ACCOUNT_NO.split("-")[0]

    @property
    def acnt_prdt_cd(self) -> str:
        """계좌상품코드 (계좌번호 뒤 2자리)"""
        return self.KIS_ACCOUNT_NO.split("-")[1]

    def assert_trading_allowed(self) -> None:
        """실전투자 진입에 필요한 이중 확인을 강제한다. 엔진 시작 시 항상 호출해야 한다."""
        if not self.IS_MOCK and not self.I_UNDERSTAND_REAL_MONEY_RISK:
            raise RuntimeError(
                "실전투자(IS_MOCK=False) 상태이지만 I_UNDERSTAND_REAL_MONEY_RISK=true가 "
                "설정되지 않았습니다. 실거래를 의도한 것이 맞다면 .env에 명시적으로 추가하세요."
            )

    # --- 해외 원주 발굴 ➔ 국내 대체 ETF 대리 매매 (Proxy Trading) ---
    OVERSEAS_PROXY_TRADING_ENABLED: bool = True  # 해외 종목 발굴 시 국장 대체 ETF로 매매

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


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

