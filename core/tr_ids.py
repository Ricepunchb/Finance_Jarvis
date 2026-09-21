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


# =====================================================================
# 해외주식 (Phase 3 — 현재는 미국(NASD/NYSE/AMEX)만 실증됨. 나머지 거래소는
# 공식 소스 기준으로 표를 채워뒀지만 아직 실거래로 검증하지 않았다.)
# =====================================================================

# --- 해외주식 주문 (order) — 거래소×매수/매도별로 완전히 불규칙하므로 명시적 표만 허용 ---
# (real_tr_id, demo_tr_id). 미국 매도의 모의투자 tr_id(VTTT1001U)는 공식 소스 최신본에서
# 정정된 값 — 실전(TTTT1006U)과 단순 V-접두 치환 관계가 아니므로 반드시 이 표를 통해서만 조회한다.
_OVERSEAS_ORDER_TR = {
    ("NASD", "buy"): ("TTTT1002U", "VTTT1002U"),
    ("NYSE", "buy"): ("TTTT1002U", "VTTT1002U"),
    ("AMEX", "buy"): ("TTTT1002U", "VTTT1002U"),
    ("NASD", "sell"): ("TTTT1006U", "VTTT1001U"),
    ("NYSE", "sell"): ("TTTT1006U", "VTTT1001U"),
    ("AMEX", "sell"): ("TTTT1006U", "VTTT1001U"),
    ("SEHK", "buy"): ("TTTS1002U", "VTTS1002U"),
    ("SEHK", "sell"): ("TTTS1001U", "VTTS1001U"),
    ("SHAA", "buy"): ("TTTS0202U", "VTTS0202U"),
    ("SHAA", "sell"): ("TTTS1005U", "VTTS1005U"),
    ("SZAA", "buy"): ("TTTS0305U", "VTTS0305U"),
    ("SZAA", "sell"): ("TTTS0304U", "VTTS0304U"),
    ("TKSE", "buy"): ("TTTS0308U", "VTTS0308U"),
    ("TKSE", "sell"): ("TTTS0307U", "VTTS0307U"),
    ("HASE", "buy"): ("TTTS0311U", "VTTS0311U"),
    ("HASE", "sell"): ("TTTS0310U", "VTTS0310U"),
    ("VNSE", "buy"): ("TTTS0311U", "VTTS0311U"),
    ("VNSE", "sell"): ("TTTS0310U", "VTTS0310U"),
}

# 거래(주문/잔고) API의 거래소코드(OVRS_EXCG_CD) -> 시세조회 API의 거래소코드(EXCD).
# 두 API 계열이 서로 다른 코드 체계를 쓴다 (예: 주문은 "NASD", 시세는 "NAS").
OVRS_EXCG_TO_QUOTE_EXCD = {
    "NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS",
    "SEHK": "HKS", "SHAA": "SHS", "SZAA": "SZS", "TKSE": "TSE",
    "HASE": "HNX", "VNSE": "HSX",
}


def order_overseas_tr_id(ovrs_excg_cd: str, side: str) -> str:
    real, demo = _OVERSEAS_ORDER_TR[(ovrs_excg_cd, side)]
    return _pick(real=real, demo=demo)


# --- 해외주식 잔고조회 (외화 기준, inquire-balance) ---
def inquire_balance_overseas_tr_id() -> str:
    return _pick(real="TTTS3012R", demo="VTTS3012R")


# --- 해외주식 체결기준현재잔고 (원화 환산, inquire-present-balance) — 국내+해외 통합비중 계산용 ---
def inquire_present_balance_tr_id() -> str:
    return _pick(real="CTRP6504R", demo="VTRP6504R")


# --- 해외주식 매수가능금액조회 (inquire-psamount) ---
def inquire_psamount_tr_id() -> str:
    return _pick(real="TTTS3007R", demo="VTTS3007R")


# --- 해외주식 정정취소 (order-rvsecncl) — 거래소 구분 없이 단일 tr_id ---
def order_rvsecncl_overseas_tr_id() -> str:
    return _pick(real="TTTT1004U", demo="VTTT1004U")


# --- 해외주식 주문체결내역조회 (inquire-ccnl, 3개월 이내) — 재기동 시 대조(reconciliation)용 ---
def inquire_ccnl_overseas_tr_id() -> str:
    return _pick(real="TTTS3035R", demo="VTTS3035R")


# --- 해외주식 실시간 체결통보 (websocket) ---
def ccnl_notice_overseas_tr_id() -> str:
    return _pick(real="H0GSCNI0", demo="H0GSCNI9")


# --- 해외주식 현재가/차트조회는 조회(quotation) 전용 API로 실전/모의 구분이 없다 ---
INQUIRE_PRICE_OVERSEAS_TR_ID = "HHDFS00000300"
INQUIRE_DAILYPRICE_OVERSEAS_TR_ID = "HHDFS76240000"
