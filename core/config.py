# core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    KIS_APP_KEY: str
    KIS_APP_SECRET: str
    KIS_ACCOUNT_NO: str    # 예: 12345678-01
    KIS_HTS_ID: str        # 실시간 체결통보(웹소켓) 구독에 필요한 HTS 로그인 ID
    IS_MOCK: bool = True   # 기본값은 모의투자

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
    STOP_LOSS_PCT: float = 0.07             # 평단가 대비 -7% 시 전량 강제매도 (쿨다운/일일손실한도 우회)
    TRAILING_TAKE_PROFIT_PCT: float = 0.05  # 진입 후 고점 대비 -5% 하락 시 전량매도

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
    NEWS_LOOKBACK_HOURS: int = 24
    NEWS_MAX_ARTICLES_PER_SYMBOL: int = 5

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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
