# core/kis_overseas.py
"""해외주식 KIS API를 감싸는 typed 함수들. 현재는 미국(NASD/NYSE/AMEX)만 실거래로
검증되었다 — 다른 거래소는 tr_id 표는 채워져 있지만 아직 실증하지 않았다.

시장가 주문은 지원하지 않는다: 데모 계정은 미국 주문에서도 지정가(00)만 허용되고,
환전이 얽힌 시장에서 가격을 특정하지 않는 주문은 리스크가 커서 의도적으로 배제했다.
모든 주문은 kis_domestic.py와 마찬가지로 rt_cd 실패를 KisApiError로 명시적으로 드러낸다.
"""
from typing import Any, Dict, List, Literal, Optional

from core import tr_ids
from core.config import settings
from core.kis_client import AsyncKISClient
from core.kis_common import KisApiError, ensure_ok as _ensure_ok

Side = Literal["buy", "sell"]


async def get_price(client: AsyncKISClient, ovrs_excg_cd: str, symbol: str) -> Dict[str, Any]:
    """해외주식 현재체결가 조회."""
    excd = tr_ids.OVRS_EXCG_TO_QUOTE_EXCD[ovrs_excg_cd]
    response = await client.request(
        method="GET",
        path="/uapi/overseas-price/v1/quotations/price",
        tr_id=tr_ids.INQUIRE_PRICE_OVERSEAS_TR_ID,
        params={"AUTH": "", "EXCD": excd, "SYMB": symbol},
    )
    return _ensure_ok(response).get("output", {})


async def get_daily_chart(
    client: AsyncKISClient, ovrs_excg_cd: str, symbol: str, base_date: str = "", modified_price: bool = True
) -> List[Dict[str, Any]]:
    """해외주식 기간별시세(일봉). base_date가 빈 문자열이면 최근부터 조회."""
    excd = tr_ids.OVRS_EXCG_TO_QUOTE_EXCD[ovrs_excg_cd]
    response = await client.request(
        method="GET",
        path="/uapi/overseas-price/v1/quotations/dailyprice",
        tr_id=tr_ids.INQUIRE_DAILYPRICE_OVERSEAS_TR_ID,
        params={
            "AUTH": "", "EXCD": excd, "SYMB": symbol,
            "GUBN": "0",  # 0: 일봉
            "BYMD": base_date,
            "MODP": "1" if modified_price else "0",
        },
    )
    return _ensure_ok(response).get("output2", [])


async def get_balance(client: AsyncKISClient, ovrs_excg_cd: str, tr_crcy_cd: str) -> Dict[str, Any]:
    """해외주식 잔고조회 (외화 기준 — 정밀한 개별 포지션 수량/평단가용)."""
    response = await client.request(
        method="GET",
        path="/uapi/overseas-stock/v1/trading/inquire-balance",
        tr_id=tr_ids.inquire_balance_overseas_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "TR_CRCY_CD": tr_crcy_cd,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        },
    )
    body = _ensure_ok(response)
    return {"holdings": body.get("output1", []), "summary": body.get("output2", {})}


async def get_present_balance_krw(client: AsyncKISClient) -> Dict[str, Any]:
    """해외주식 체결기준현재잔고 — 원화(KRW) 환산. 국내+해외 통합 총자산 계산에 사용.

    NATN_CD="000"(전체 국가), TR_MKET_CD="00"(전체 시장)로 보유한 모든 해외주식을 한 번에 조회한다.
    """
    response = await client.request(
        method="GET",
        path="/uapi/overseas-stock/v1/trading/inquire-present-balance",
        tr_id=tr_ids.inquire_present_balance_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "WCRC_FRCR_DVSN_CD": "01",  # 01: 원화
            "NATN_CD": "000",
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        },
    )
    body = _ensure_ok(response)
    return {
        "holdings": body.get("output1", []),
        "account_summary": body.get("output2", []),
        "totals": (body.get("output3") or [{}])[0] if isinstance(body.get("output3"), list) else body.get("output3", {}),
    }


async def get_buyable_cash(
    client: AsyncKISClient, ovrs_excg_cd: str, symbol: str, price: float
) -> Dict[str, Any]:
    """해외주식 매수가능금액조회. 주문 직전에 항상 새로 조회해서 사용해야 한다."""
    response = await client.request(
        method="GET",
        path="/uapi/overseas-stock/v1/trading/inquire-psamount",
        tr_id=tr_ids.inquire_psamount_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "OVRS_ORD_UNPR": str(price),
            "ITEM_CD": symbol,
        },
    )
    return _ensure_ok(response).get("output", {})


async def order(
    client: AsyncKISClient,
    ovrs_excg_cd: str,
    symbol: str,
    side: Side,
    qty: int,
    price: float,
) -> Dict[str, Any]:
    """해외주식 주문 (지정가 전용). price는 반드시 지정해야 한다 (시장가 미지원)."""
    tr_id = tr_ids.order_overseas_tr_id(ovrs_excg_cd, side)
    response = await client.request(
        method="POST",
        path="/uapi/overseas-stock/v1/trading/order",
        tr_id=tr_id,
        data={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price),
            "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "",
            "SLL_TYPE": "00" if side == "sell" else "",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # 00: 지정가 (모의투자/데모는 이 값만 허용)
        },
    )
    return _ensure_ok(response).get("output", {})


async def cancel_order(
    client: AsyncKISClient, ovrs_excg_cd: str, symbol: str, orgn_odno: str, qty: int, price: float
) -> Dict[str, Any]:
    """미체결 주문 취소."""
    response = await client.request(
        method="POST",
        path="/uapi/overseas-stock/v1/trading/order-rvsecncl",
        tr_id=tr_ids.order_rvsecncl_overseas_tr_id(),
        data={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": symbol,
            "ORGN_ODNO": orgn_odno,
            "RVSE_CNCL_DVSN_CD": "02",  # 02: 취소
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price),
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        },
    )
    return _ensure_ok(response).get("output", {})


async def get_ccnl(
    client: AsyncKISClient, start_date: str, end_date: str, ovrs_excg_cd: str = "%"
) -> List[Dict[str, Any]]:
    """해외주식 주문체결내역조회 (3개월 이내). 재기동 시 order_intents 대조(reconciliation)에 사용.

    모의투자에서는 sll_buy_dvsn/ccld_nccs_dvsn/ovrs_excg_cd를 "전체"로만 조회 가능하다.
    """
    response = await client.request(
        method="GET",
        path="/uapi/overseas-stock/v1/trading/inquire-ccnl",
        tr_id=tr_ids.inquire_ccnl_overseas_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "PDNO": "%",
            "ORD_STRT_DT": start_date,
            "ORD_END_DT": end_date,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "00",
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "SORT_SQN": "DS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        },
    )
    return _ensure_ok(response).get("output", [])
