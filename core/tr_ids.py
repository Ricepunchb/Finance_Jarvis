# core/tr_ids.py
"""KIS 국내주식 tr_id를 실전/모의투자별로 명시적으로 관리한다.

해외주식은 메리츠증권 Open API(core/meritz_overseas.py)로 완전히 분리·일원화되었으며,
이 모듈은 한국투자증권(KIS) 국내주식 전용 TR ID만 담당한다.
"""
from core.config import settings


def _pick(real: str, demo: str) -> str:
    return demo if settings.IS_MOCK else real


# --- 국내주식 주문 (order-cash) ---
def order_cash_buy_tr_id() -> str:
    return _pick(real="TTTC0012U", demo="VTTC0012U")


def order_cash_sell_tr_id() -> str:
    return _pick(real="TTTC0011U", demo="VTTC0011U")


# --- 국내주식 잔고조회 (inquire-balance) ---
def inquire_balance_tr_id() -> str:
    return _pick(real="TTTC8434R", demo="VTTC8434R")


# --- 국내주식 매수가능금액조회 (inquire-psbl-order) ---
def inquire_psbl_order_tr_id() -> str:
    return _pick(real="TTTC8908R", demo="VTTC8908R")


# --- 국내주식 매도가능수량조회 (inquire-psbl-sell) ---
# 실전/모의 구분 없이 단일 tr_id를 사용한다 (공식 예제에 데모 전용 변형이 없음을 확인).
INQUIRE_PSBL_SELL_TR_ID = "TTTC8408R"


# --- 국내주식 정정취소 (order-rvsecncl) ---
def order_rvsecncl_tr_id() -> str:
    return _pick(real="TTTC0013U", demo="VTTC0013U")


# --- 국내주식 정정취소가능주문조회 (inquire-psbl-rvsecncl) ---
# 실전/모의 구분 없이 단일 tr_id 사용 (공식 예제에 데모 전용 변형 없음을 확인).
INQUIRE_PSBL_RVSECNCL_TR_ID = "TTTC0084R"


# --- 국내주식 일별주문체결조회 (inquire-daily-ccld, 3개월 이내) — 재기동 시 대조(reconciliation)용 ---
def inquire_daily_ccld_tr_id() -> str:
    return _pick(real="TTTC0081R", demo="VTTC0081R")


# --- 국내주식 실시간 체결통보 (websocket) ---
def ccnl_notice_tr_id() -> str:
    return _pick(real="H0STCNI0", demo="H0STCNI9")


# --- 국내주식 기간별계좌권리현황조회 (inquire-period-rights, 배당/권리) ---
INQUIRE_PERIOD_RIGHTS_TR_ID = "CTRGA011R"


# --- 국내주식 현재가시세/차트/VI조회는 조회(quotation) 전용 API로 실전/모의 구분이 없다 ---
INQUIRE_PRICE_TR_ID = "FHKST01010100"
INQUIRE_DAILY_ITEMCHARTPRICE_TR_ID = "FHKST03010100"
INQUIRE_VI_STATUS_TR_ID = "FHPST01390000"

# --- 국내주식 분봉조회 (당일/과거) — 조회 전용, 모의투자에서도 동일 tr_id로 확인됨 ---
INQUIRE_TIME_ITEMCHARTPRICE_TR_ID = "FHKST03010200"       # 당일 분봉만, 최대 30건/회
INQUIRE_TIME_DAILYCHARTPRICE_TR_ID = "FHKST03010230"      # 과거 분봉, 최대 120건/회

# --- 국내주식 투자의견/재무비율 — 조회 전용, 모의투자에서도 동일 tr_id로 확인됨 ---
INVEST_OPINION_TR_ID = "FHKST663300C0"
FINANCE_FINANCIAL_RATIO_TR_ID = "FHKST66430300"

# --- 국내주식 순위분석/시황 뉴스 — 조회 전용 (종목 발굴 소스) ---
RANKING_FLUCTUATION_TR_ID = "FHPST01700000"
RANKING_VOLUME_TR_ID = "FHPST01710000"
NEWS_TITLE_TR_ID = "FHKST01011800"
