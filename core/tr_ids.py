# core/tr_ids.py
"""KIS tr_id를 실전/모의투자별로 명시적으로 관리한다.

해외주식은 거래소별로 tr_id가 완전히 불규칙(문자열 치환으로 유도 불가)하기 때문에,
국내주식도 같은 원칙으로 처음부터 lookup 딕셔너리로 관리해 나중에 해외를 추가할 때
설계를 바꾸지 않도록 한다.
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


# --- 국내주식 현재가시세/차트/VI조회는 조회(quotation) 전용 API로 실전/모의 구분이 없다 ---
INQUIRE_PRICE_TR_ID = "FHKST01010100"
INQUIRE_DAILY_ITEMCHARTPRICE_TR_ID = "FHKST03010100"
INQUIRE_VI_STATUS_TR_ID = "FHPST01390000"
