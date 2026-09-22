# core/kis_domestic.py
"""국내주식(KOSPI/KOSDAQ) KIS API를 감싸는 typed 함수들.

모든 함수는 AsyncKISClient 인스턴스를 받아 dict를 그대로 반환한다 (raw KIS 응답의
output/output1/output2를 꺼내는 정도만 처리). 실패 판단(rt_cd)은 호출자가 명시적으로
KisApiError 를 통해 확인할 수 있게 한다 — 조용히 삼키는 에러 처리는 자동매매에서
가장 위험한 패턴이기 때문.
"""
from typing import Any, Dict, List, Literal, Optional

from core import tr_ids
from core.config import settings
from core.kis_client import AsyncKISClient
from core.kis_common import KisApiError, ensure_ok as _ensure_ok

Side = Literal["buy", "sell"]

__all__ = ["KisApiError", "Side"]  # kis_overseas.py 등 다른 모듈이 그대로 재사용


async def get_price(client: AsyncKISClient, symbol: str) -> Dict[str, Any]:
    """주식 현재가 시세 조회."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id=tr_ids.INQUIRE_PRICE_TR_ID,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
    )
    return _ensure_ok(response).get("output", {})


async def get_daily_chart(
    client: AsyncKISClient,
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "D",
) -> List[Dict[str, Any]]:
    """국내주식 기간별(일/주/월/년) 시세. 지표 계산용 OHLCV를 반환한다."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        tr_id=tr_ids.INQUIRE_DAILY_ITEMCHARTPRICE_TR_ID,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "1",
        },
    )
    return _ensure_ok(response).get("output2", [])


async def get_vi_status(client: AsyncKISClient, symbol: str) -> List[Dict[str, Any]]:
    """종목별 변동성완화장치(VI) 발동 현황. 비어 있으면 VI 미발동."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/quotations/inquire-vi-status",
        tr_id=tr_ids.INQUIRE_VI_STATUS_TR_ID,
        params={
            "FID_DIV_CLS_CODE": "0",
            "FID_COND_SCR_DIV_CODE": "20139",
            "FID_MRKT_CLS_CODE": "0",
            "FID_INPUT_ISCD": symbol,
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_INPUT_DATE_1": "",
            "FID_TRGT_CLS_CODE": "",
            "FID_TRGT_EXLS_CLS_CODE": "",
        },
    )
    return _ensure_ok(response).get("output", [])


async def get_minute_chart_today(
    client: AsyncKISClient, symbol: str, hour_1: str, include_past: str = "Y",
) -> List[Dict[str, Any]]:
    """당일 분봉조회. 당일 데이터만 최대 30건/회, 최신순으로 반환된다.

    hour_1(HHMMSS)을 과거로 옮겨가며 호출하면 더 이전 구간을 페이지네이션할 수 있다.
    """
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        tr_id=tr_ids.INQUIRE_TIME_ITEMCHARTPRICE_TR_ID,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": hour_1,
            "FID_PW_DATA_INCU_YN": include_past,
            "FID_ETC_CLS_CODE": "",
        },
    )
    return _ensure_ok(response).get("output2", [])


async def get_minute_chart_historical(
    client: AsyncKISClient, symbol: str, date_1: str, hour_1: str = "153000",
) -> List[Dict[str, Any]]:
    """과거 분봉조회(최대 120건/회, 최신순). date_1(YYYYMMDD)일자 기준 hour_1(HHMMSS)
    이전 구간을 반환한다 — 여러 날짜로 반복 호출해 백필한다."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
        tr_id=tr_ids.INQUIRE_TIME_DAILYCHARTPRICE_TR_ID,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": hour_1,
            "FID_INPUT_DATE_1": date_1,
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "",
        },
    )
    return _ensure_ok(response).get("output2", [])


async def get_invest_opinion(
    client: AsyncKISClient, symbol: str, date_from: str, date_to: str,
) -> List[Dict[str, Any]]:
    """증권사별 투자의견(목표주가 포함) 조회. 집계값이 아니라 개별 리포트 리스트."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/quotations/invest-opinion",
        tr_id=tr_ids.INVEST_OPINION_TR_ID,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "16633",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": date_from,
            "FID_INPUT_DATE_2": date_to,
        },
    )
    return _ensure_ok(response).get("output", [])


async def get_financial_ratio(client: AsyncKISClient, symbol: str, div_cls: str = "0") -> List[Dict[str, Any]]:
    """분기/연간 재무비율(ROE/부채비율/성장률 등) 조회. 최신 순으로 여러 기(期) 반환."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/finance/financial-ratio",
        tr_id=tr_ids.FINANCE_FINANCIAL_RATIO_TR_ID,
        params={
            "FID_DIV_CLS_CODE": div_cls,
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": symbol,
        },
    )
    return _ensure_ok(response).get("output", [])


async def get_balance(client: AsyncKISClient) -> Dict[str, Any]:
    """계좌 잔고조회. {"holdings": [...], "summary": {...}} 형태로 반환한다."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id=tr_ids.inquire_balance_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",  # 종목별
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",  # 전일매매포함
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    body = _ensure_ok(response)
    return {"holdings": body.get("output1", []), "summary": (body.get("output2") or [{}])[0]}


async def get_buyable_cash(
    client: AsyncKISClient, symbol: str, price: int, order_dvsn: str = "01"
) -> Dict[str, Any]:
    """매수가능금액조회. 주문 직전에 항상 새로 조회해서 사용해야 한다 (캐시 금지)."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        tr_id=tr_ids.inquire_psbl_order_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_UNPR": str(price),
            "ORD_DVSN": order_dvsn,
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        },
    )
    return _ensure_ok(response).get("output", {})


async def get_sellable_qty(client: AsyncKISClient, symbol: str) -> Dict[str, Any]:
    """매도가능수량조회. 매도 주문 직전에 항상 새로 조회해서 사용해야 한다 (캐시 금지)."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/trading/inquire-psbl-sell",
        tr_id=tr_ids.INQUIRE_PSBL_SELL_TR_ID,
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "PDNO": symbol,
        },
    )
    return _ensure_ok(response).get("output", {})


async def order_cash(
    client: AsyncKISClient,
    symbol: str,
    side: Side,
    qty: int,
    price: Optional[int] = None,
) -> Dict[str, Any]:
    """현금 주식 주문. price=None이면 시장가(01), 지정하면 지정가(00).

    반환값은 KIS output ({"ODNO": ..., "ORD_TMD": ..., ...}) — 호출자는 반드시
    이 ODNO를 order_intents에 즉시 기록해야 한다 (idempotency의 유일한 근거).
    """
    order_dvsn = "01" if price is None else "00"
    tr_id = tr_ids.order_cash_buy_tr_id() if side == "buy" else tr_ids.order_cash_sell_tr_id()
    response = await client.request(
        method="POST",
        path="/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=tr_id,
        data={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": order_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price) if price is not None else "0",
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if side == "sell" else "",
            "CNDT_PRIC": "",
        },
    )
    return _ensure_ok(response).get("output", {})


async def get_cancelable_orders(client: AsyncKISClient) -> List[Dict[str, Any]]:
    """정정취소가능주문조회. 긴급정지(전량취소) 시 취소 대상 목록을 얻는 데 사용."""
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
        tr_id=tr_ids.INQUIRE_PSBL_RVSECNCL_TR_ID,
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    return _ensure_ok(response).get("output", [])


async def cancel_order(
    client: AsyncKISClient,
    symbol: str,
    krx_fwdg_ord_orgno: str,
    orgn_odno: str,
) -> Dict[str, Any]:
    """미체결 주문 전량취소."""
    response = await client.request(
        method="POST",
        path="/uapi/domestic-stock/v1/trading/order-rvsecncl",
        tr_id=tr_ids.order_rvsecncl_tr_id(),
        data={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno,
            "ORGN_ODNO": orgn_odno,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02: 취소
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",  # 잔량 전부 취소
            "EXCG_ID_DVSN_CD": "KRX",
        },
    )
    return _ensure_ok(response).get("output", {})


async def get_daily_ccld(
    client: AsyncKISClient,
    start_date: str,
    end_date: str,
    symbol: str = "",
) -> List[Dict[str, Any]]:
    """일별주문체결조회 (3개월 이내). 재기동 시 order_intents 대조(reconciliation)에 사용.

    ccld_dvsn="00"(전체: 체결+미체결) 으로 조회해야 미체결 주문도 놓치지 않는다.
    """
    response = await client.request(
        method="GET",
        path="/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        tr_id=tr_ids.inquire_daily_ccld_tr_id(),
        params={
            "CANO": settings.cano,
            "ACNT_PRDT_CD": settings.acnt_prdt_cd,
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": "00",  # 00: 전체
            "PDNO": symbol,
            "CCLD_DVSN": "00",  # 00: 전체(체결+미체결)
            "INQR_DVSN": "00",
            "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    return _ensure_ok(response).get("output1", [])
