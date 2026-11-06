# core/meritz_overseas.py
"""메리츠증권 해외주식 Open API 래퍼 모듈 (미국 NASD/NYSE/AMEX 전용).

기존 엔진/인디케이터/대조(reconciliation) 파이프라인과 100% 호환되도록
메리츠 응답 필드를 표준화된 딕셔너리 포맷으로 변환하여 반환한다.
또한 SHADOW_MODE(드라이런) 활성화 시 실제 주문/취소 POST 요청을 전송하지 않고
안전하게 가상 주문 응답을 생성한다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

from core.config import settings
from core.meritz_client import AsyncMeritzClient, MeritzApiError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
Side = Literal["buy", "sell"]

# 시세 조회용 거래소 코드 (mrkt_cls_code)
EXCG_TO_MRKT_CLS: Dict[str, str] = {
    "NASD": "OQ",
    "NASDAQ": "OQ",
    "NAS": "OQ",
    "OQ": "OQ",
    "NYSE": "NY",
    "NYS": "NY",
    "NY": "NY",
    "AMEX": "AX",
    "AMS": "AX",
    "AX": "AX",
}

# 주문/잔고용 해외거래소벤더식별번호 (ovrs_exch_vndr_idno)
EXCG_TO_VNDR_IDNO: Dict[str, str] = {
    "NASD": "0537",
    "NASDAQ": "0537",
    "NAS": "0537",
    "OQ": "0537",
    "NYSE": "0321",
    "NYS": "0321",
    "NY": "0321",
    "AMEX": "0066",
    "AMS": "0066",
    "AX": "0066",
}

VNDR_IDNO_TO_EXCG: Dict[str, str] = {
    "0537": "NASD",
    "0321": "NYSE",
    "0066": "AMEX",
}

# 해외 주문 경고 중 warn_cnfr_yn="Y" 재전송으로 접수 가능한 코드 (1: 일반경고, 3: 미국 PTP 과세 확인)
OVERSEAS_RESEND_WARN_CODES = {"1", "3"}


def _resolve_mrkt_cls(ovrs_excg_cd: str) -> str:
    code = (ovrs_excg_cd or "NASD").strip().upper()
    if code not in EXCG_TO_MRKT_CLS:
        raise ValueError(f"지원하지 않는 해외 거래소 코드: {ovrs_excg_cd}")
    return EXCG_TO_MRKT_CLS[code]


def _resolve_vndr_idno(ovrs_excg_cd: str) -> str:
    code = (ovrs_excg_cd or "NASD").strip().upper()
    if code not in EXCG_TO_VNDR_IDNO:
        raise ValueError(f"지원하지 않는 해외 거래소 코드: {ovrs_excg_cd}")
    return EXCG_TO_VNDR_IDNO[code]


def _format_order_price(price: float) -> str:
    """메리츠 해외 주문 단가는 소수점 이하 4자리까지 허용 (초과 시 IGW50002 거절)."""
    formatted = f"{float(price):.4f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


async def get_price(client: AsyncMeritzClient, ovrs_excg_cd: str, symbol: str) -> Dict[str, Any]:
    """해외주식 현재가 조회 (`GET /market/v1/overseas/prices`)."""
    mrkt_cls = _resolve_mrkt_cls(ovrs_excg_cd)
    clean_sym = symbol.strip().upper()
    response = await client.request(
        method="GET",
        path="/market/v1/overseas/prices",
        params={
            "mrkt_div_code": "OV",
            "mrkt_cls_code": mrkt_cls,
            "iscd": clean_sym,
            "dely_rltm_cls_code": "0",
        },
    )
    data = response.get("data") or {}
    if not isinstance(data, dict):
        return {}
    prpr = data.get("prpr") or data.get("ovrs_nmix_prpr") or "0"
    kor_name = data.get("kor_isnm") or data.get("hts_kor_isnm") or data.get("isnm") or ""
    return {
        **data,
        "last": str(prpr),
        "ovrs_nmix_prpr": str(prpr),
        "hts_kor_isnm": kor_name,
        "name": kor_name,
        "currency": data.get("crnc_code") or "USD",
    }


async def get_daily_chart(
    client: AsyncMeritzClient,
    ovrs_excg_cd: str,
    symbol: str,
    base_date: str = "",
    modified_price: bool = True,
    from_date: str = "",
    to_date: str = "",
) -> List[Dict[str, Any]]:
    """해외주식 일봉 시세추이 조회 (`GET /market/v1/overseas/candles/days`).

    base_date가 주어지면 해당 날짜를 종료일(to)로 하여 과거 120일 구간을 조회한다.
    반환되는 각 행은 indicators.chart_rows_to_dataframe_overseas()와 즉시 호환된다.
    """
    mrkt_cls = _resolve_mrkt_cls(ovrs_excg_cd)
    clean_sym = symbol.strip().upper()

    params: Dict[str, str] = {
        "mrkt_div_code": "OV",
        "mrkt_cls_code": mrkt_cls,
        "iscd": clean_sym,
        "dely_rltm_cls_code": "0",
        "mod_stpr_cls_code": "0" if modified_price else "1",
    }
    if to_date or base_date:
        end_str = (to_date or base_date).strip()
        params["to"] = end_str
        if from_date:
            params["from"] = from_date.strip()
        else:
            try:
                dt_end = datetime.strptime(end_str, "%Y%m%d")
                params["from"] = (dt_end - timedelta(days=140)).strftime("%Y%m%d")
            except ValueError:
                pass
    elif from_date:
        params["from"] = from_date.strip()
        params["to"] = datetime.now(tz=KST).strftime("%Y%m%d")

    response = await client.request(
        method="GET",
        path="/market/v1/overseas/candles/days",
        params=params,
    )
    rows = response.get("data") or []
    if not isinstance(rows, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d_val = r.get("date") or r.get("xymd") or ""
        oprc = r.get("oprc") if r.get("oprc") is not None else r.get("open", "0")
        hprc = r.get("hprc") if r.get("hprc") is not None else r.get("high", "0")
        lprc = r.get("lprc") if r.get("lprc") is not None else r.get("low", "0")
        prpr = r.get("prpr") if r.get("prpr") is not None else r.get("clos", "0")
        vol = r.get("acml_vol") if r.get("acml_vol") is not None else r.get("tvol", "0")
        normalized.append(
            {
                **r,
                "date": str(d_val),
                "xymd": str(d_val),
                "open": str(oprc),
                "high": str(hprc),
                "low": str(lprc),
                "clos": str(prpr),
                "tvol": str(vol),
            }
        )
    return normalized


async def get_minute_chart(
    client: AsyncMeritzClient,
    ovrs_excg_cd: str,
    symbol: str,
    n_min: str = "30",
    include_prev_day: bool = True,
    next_page: str = "",
    keyb: str = "",
) -> List[Dict[str, Any]]:
    """해외주식 분봉 시세추이 조회 (`GET /market/v1/overseas/candles/minutes`).

    hour_cls_code는 초 단위로 입력받으므로 n_min="30" -> "1800"으로 변환한다.
    """
    mrkt_cls = _resolve_mrkt_cls(ovrs_excg_cd)
    clean_sym = symbol.strip().upper()
    try:
        seconds = max(60, int(n_min) * 60)
    except (TypeError, ValueError):
        seconds = 1800

    response = await client.request(
        method="GET",
        path="/market/v1/overseas/candles/minutes",
        params={
            "mrkt_div_code": "OV",
            "mrkt_cls_code": mrkt_cls,
            "iscd": clean_sym,
            "dely_rltm_cls_code": "0",
            "hour_cls_code": str(seconds),
        },
    )
    rows = response.get("data") or []
    if not isinstance(rows, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        kymd = r.get("korea_date") or r.get("kymd") or ""
        khms = r.get("korea_hour") or r.get("khms") or ""
        oprc = r.get("oprc") if r.get("oprc") is not None else r.get("open", "0")
        hprc = r.get("hprc") if r.get("hprc") is not None else r.get("high", "0")
        lprc = r.get("lprc") if r.get("lprc") is not None else r.get("low", "0")
        prpr = r.get("prpr") if r.get("prpr") is not None else r.get("last", "0")
        evol = r.get("cntg_vol") if r.get("cntg_vol") is not None else r.get("evol", "0")
        normalized.append(
            {
                **r,
                "kymd": str(kymd),
                "khms": str(khms),
                "open": str(oprc),
                "high": str(hprc),
                "low": str(lprc),
                "last": str(prpr),
                "evol": str(evol),
            }
        )
    return normalized


async def get_holdings(client: AsyncMeritzClient) -> List[Dict[str, Any]]:
    """해외주식 보유잔고 조회 (`GET /accounts/v1/me/overseas/holdings`)."""
    all_holdings: List[Dict[str, Any]] = []
    tr_cont = ""
    tr_cont_key = ""

    for _ in range(5):
        params: Dict[str, str] = {}
        if tr_cont:
            params["tr_cont"] = tr_cont
        if tr_cont_key:
            params["tr_cont_key"] = tr_cont_key

        response = await client.request(
            method="GET",
            path="/accounts/v1/me/overseas/holdings",
            params=params or None,
        )
        rows = response.get("data") or []
        if isinstance(rows, list):
            for h in rows:
                if not isinstance(h, dict):
                    continue
                sym = (h.get("ovrs_stck_symb_iscd") or h.get("pdno") or "").strip()
                vndr = str(h.get("ovrs_exch_vndr_idno") or "0537").strip()
                excg = VNDR_IDNO_TO_EXCG.get(vndr, h.get("ovrs_excg_cd") or "NASD")
                all_holdings.append(
                    {
                        **h,
                        "pdno": sym,
                        "ovrs_excg_cd": excg,
                        "cblc_qty13": str(h.get("rmnd_dqty") if h.get("rmnd_dqty") is not None else h.get("cblc_qty13", "0")),
                        "ord_psbl_qty1": str(h.get("seln_able_dqty") if h.get("seln_able_dqty") is not None else h.get("ord_psbl_qty1", "0")),
                        "avg_unpr3": str(h.get("avrg_unpr") if h.get("avrg_unpr") is not None else h.get("avg_unpr3", "0")),
                        "crcy_cd": h.get("crcd") or h.get("crcy_cd") or "USD",
                        "bass_exrt": str(h.get("exrt") if h.get("exrt") is not None else h.get("bass_exrt", "0")),
                    }
                )

        if str(response.get("rsp_cd")) == "5762" and response.get("tr_cont_key"):
            tr_cont = "1"
            tr_cont_key = str(response["tr_cont_key"])
        else:
            break

    return all_holdings


async def get_currencies(client: AsyncMeritzClient) -> List[Dict[str, Any]]:
    """해외주식 외화잔고/자산평가 조회 (`GET /accounts/v1/me/overseas/currencies`)."""
    response = await client.request(
        method="GET",
        path="/accounts/v1/me/overseas/currencies",
    )
    rows = response.get("data") or []
    return rows if isinstance(rows, list) else []


async def get_present_balance_krw(client: AsyncMeritzClient) -> Dict[str, Any]:
    """해외주식 현재잔고 및 자산 통합 조회 (USD 기준 및 KRW 환산치 병행).

    메리츠 해외 전용 계좌이므로 기본 tot_asst_amt는 순수 USD 총자산으로 반환한다.
    원화 환산치는 tot_asst_krw 필드로 제공한다.
    """
    holdings = await get_holdings(client)
    currencies = await get_currencies(client)

    tot_asst_krw = 0.0
    tot_asst_usd = 0.0
    usd_exrt = 0.0
    usd_cash = 0.0
    usd_avail = 0.0

    for c in currencies:
        if not isinstance(c, dict):
            continue
        crcd = c.get("crcd")
        exrt = float(c.get("vltn_exrt") or 0.0)
        entm = float(c.get("entm_aset_vltn_amt") or 0.0)
        frcr = float(c.get("frcr_amt") or 0.0)
        estm = float(c.get("estm_frcr_amt") or 0.0)

        if crcd == "USD":
            if exrt > 0:
                usd_exrt = exrt
            usd_cash = frcr
            usd_avail = estm
            if entm > 0:
                tot_asst_usd = entm
            if exrt > 0:
                tot_asst_krw += entm * exrt
            else:
                wncr = float(c.get("excl_wncr_vltn_amt") or 0.0)
                stck = float(c.get("ovrs_stck_vltn_amt") or 0.0)
                tot_asst_krw += wncr + stck
        elif entm > 0 and exrt > 0:
            tot_asst_krw += entm * exrt

    if tot_asst_usd <= 0 and holdings:
        stock_eval_usd = sum(
            float(h.get("cblc_qty13") or 0.0) * float(h.get("psnt_prc") or h.get("avg_unpr3") or 0.0)
            for h in holdings
        )
        tot_asst_usd = stock_eval_usd + usd_cash

    if tot_asst_krw <= 0 and tot_asst_usd > 0:
        tot_asst_krw = tot_asst_usd * (usd_exrt or 1342.0)

    return {
        "holdings": holdings,
        "account_summary": currencies,
        "totals": {
            "tot_asst_amt": str(round(tot_asst_usd, 2)),
            "tot_asst_usd": str(round(tot_asst_usd, 2)),
            "tot_asst_krw": str(round(tot_asst_krw, 2)),
            "cash_usd": str(round(usd_cash, 2)),
            "avail_usd": str(round(usd_avail, 2)),
            "bass_exrt": str(usd_exrt) if usd_exrt > 0 else "",
            "currency": "USD",
        },
    }


async def get_balance(client: AsyncMeritzClient, ovrs_excg_cd: str = "NASD", tr_crcy_cd: str = "USD") -> Dict[str, Any]:
    """해외주식 잔고조회 (보유종목 + 외화요약)."""
    present = await get_present_balance_krw(client)
    return {"holdings": present["holdings"], "summary": present["totals"]}


async def get_buyable_cash(
    client: AsyncMeritzClient, ovrs_excg_cd: str, symbol: str, price: float
) -> Dict[str, Any]:
    """해외주식 주문가능금액 조회 (`GET /trading/v1/overseas/orders/estimate`).

    원화 자동환전을 원천 차단하기 위해 intg_marg_iqry_yn="N" (통합증거금 제외, 일반 외화 기준)으로만 조회한다.
    타통화(원화) 증거금은 배제하고 순수 USD 주문가능금액(oder_crnc_oder_able_camt)만 반환한다.
    """
    response = await client.request(
        method="GET",
        path="/trading/v1/overseas/orders/estimate",
        params={"intg_marg_iqry_yn": "N"},
    )
    rows = response.get("data") or []
    usd_row: Dict[str, Any] = {}
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("crcd") == "USD":
                usd_row = r
                break
        if not usd_row and rows and isinstance(rows[0], dict):
            usd_row = rows[0]

    exrt = float(usd_row.get("exrt") or 0.0)
    # 순수 USD 주문가능액 (원화 증거금 배제)
    able_usd = float(usd_row.get("oder_crnc_oder_able_camt") or 0.0)
    if able_usd <= 0 and float(usd_row.get("frcr_amt") or 0.0) > 0:
        able_usd = float(usd_row.get("frcr_amt") or 0.0)
    max_qty = int(able_usd // float(price)) if price and float(price) > 0 else 0

    return {
        **usd_row,
        "exrt": str(exrt) if exrt > 0 else "0",
        "ovrs_ord_psbl_amt": str(able_usd),
        "max_ord_psbl_qty": str(max_qty),
        "othr_crnc_evdn_camt": "0.0000",  # 원화 증거금 0 고정
    }


async def order(
    client: AsyncMeritzClient,
    ovrs_excg_cd: str,
    symbol: str,
    side: Side,
    qty: int,
    price: float,
    shadow: Optional[bool] = None,
    available_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """해외주식 지정가 주문 (`POST /trading/v1/overseas/orders/buy` | `sell`).

    - SHADOW_MODE=True이면 실제 브로커에 전송하지 않고 가상 주문번호(SHADOW_*)를 반환한다.
    - 매수 주문 시 MAX_ORDER_NOTIONAL_USD 하드캡을 검증한다.
    - [원화 자동환전 원천 방지 가드]: 매수 시 실시간 순수 USD 가용 예수금을 초과하는 주문은 즉시 차단한다.
    - warn_cls_code가 "1" 또는 "3"이면 warn_cnfr_yn="Y"로 1회 자동 재전송한다.
    """
    is_shadow = settings.SHADOW_MODE if shadow is None else shadow
    clean_sym = symbol.strip().upper()
    vndr_idno = _resolve_vndr_idno(ovrs_excg_cd)
    price_str = _format_order_price(price)

    if side == "buy":
        notional_usd = float(qty) * float(price)
        if settings.MAX_ORDER_NOTIONAL_USD > 0 and notional_usd > settings.MAX_ORDER_NOTIONAL_USD + 1e-6:
            raise MeritzApiError(
                rsp_cd="MAX_NOTIONAL_EXCEEDED",
                message=(
                    f"1회 해외 주문 한도(${settings.MAX_ORDER_NOTIONAL_USD:,.2f}) 초과: "
                    f"{clean_sym} {qty}주 @ ${price} = ${notional_usd:,.2f}"
                ),
            )
        # [원화 자동환전 방지 가드]
        # 주문 직전 순수 USD 주문가능금액을 실시간 검증하여, 보유 달러를 초과하면 즉시 차단
        if not is_shadow:
            if available_usd is not None:
                avail_usd = available_usd
            else:
                try:
                    probe = await get_buyable_cash(client, ovrs_excg_cd, clean_sym, price)
                    avail_usd = float(probe.get("ovrs_ord_psbl_amt") or 0.0)
                except Exception as e:
                    logger.warning(f"매수 전 순수 USD 잔고 가드 검사 중 경고: {e}")
                    avail_usd = None
            if avail_usd is not None and notional_usd > avail_usd + 1e-4:
                raise MeritzApiError(
                    rsp_cd="NO_USD_CASH_AUTO_FX_PREVENTED",
                    message=(
                        f"[원화 자동환전 방지] 주문 금액(${notional_usd:,.2f})이 "
                        f"순수 USD 가용 예수금(${avail_usd:,.2f})을 초과하여 주문을 차단했습니다. "
                        f"(원화 통합증거금 자동환전 매수 원천 차단)"
                    ),
                )

    if is_shadow:
        shadow_odno = f"SHADOW_{int(time.time() * 1000)}"
        logger.info(
            f"[SHADOW MODE] 해외 가상 주문 기록 (실전 미전송): {side.upper()} {clean_sym} "
            f"{qty}주 @ ${price_str} (exch={ovrs_excg_cd}, odno={shadow_odno})"
        )
        return {
            "ODNO": shadow_odno,
            "odno": shadow_odno,
            "oder_no": shadow_odno,
            "warn_cls_code": "0",
            "shadow": True,
        }

    path = "/trading/v1/overseas/orders/buy" if side == "buy" else "/trading/v1/overseas/orders/sell"
    payload = {
        "ovrs_stck_symb_iscd": clean_sym,
        "ovrs_exch_vndr_idno": vndr_idno,
        "oder_cls_code": "01",       # 01: 지정가
        "oder_cond_cls_code": "0",   # 0: 조건 없음
        "odqt": int(qty),
        "ovrs_stck_oder_prc": price_str,
        "warn_cnfr_yn": "N",
    }

    response = await client.request(method="POST", path=path, data=payload)
    data = response.get("data") or {}
    warn_code = str(data.get("warn_cls_code") or response.get("warn_cls_code") or "0").strip()

    if warn_code != "0":
        warn_msg = str(data.get("warn_msg") or response.get("rsp_sub_msg") or "").strip()
        if warn_code in OVERSEAS_RESEND_WARN_CODES:
            logger.warning(
                f"메리츠 해외주문 경고 확인 재전송 ({clean_sym}, warn_cls_code={warn_code}): {warn_msg}"
            )
            payload["warn_cnfr_yn"] = "Y"
            response = await client.request(method="POST", path=path, data=payload)
            data = response.get("data") or {}
            warn_code = str(data.get("warn_cls_code") or response.get("warn_cls_code") or "0").strip()

        if warn_code != "0":
            raise MeritzApiError(
                rsp_cd="ORDER_NOT_ACCEPTED",
                message=f"주문이 접수되지 않았습니다 (warn_cls_code={warn_code}): {warn_msg}",
                raw=response,
                warn_cls_code=warn_code,
                resendable=warn_code in OVERSEAS_RESEND_WARN_CODES,
            )

    odno = str(data.get("oder_no") or data.get("odno") or data.get("ODNO") or "")
    return {
        **data,
        "ODNO": odno,
        "odno": odno,
    }


async def cancel_order(
    client: AsyncMeritzClient,
    ovrs_excg_cd: str,
    symbol: str,
    orgn_odno: str,
    qty: int,
    price: float = 0.0,
    oder_date: str = "",
    shadow: Optional[bool] = None,
) -> Dict[str, Any]:
    """해외주식 미체결 주문 취소 (`POST /trading/v1/overseas/orders/cancel`)."""
    is_shadow = settings.SHADOW_MODE if shadow is None else shadow
    if is_shadow or str(orgn_odno).startswith("SHADOW_"):
        shadow_odno = f"SHADOW_CNCL_{int(time.time() * 1000)}"
        logger.info(f"[SHADOW MODE] 해외 가상 주문취소: orgn_odno={orgn_odno}, symbol={symbol}, qty={qty}")
        return {"ODNO": shadow_odno, "odno": shadow_odno, "oder_no": shadow_odno, "shadow": True}

    date_str = oder_date or datetime.now(tz=KST).strftime("%Y%m%d")
    payload = {
        "oder_date": date_str,
        "orgl_oder_no": int(orgn_odno),
        "oder_cls_code": "01",
        "odqt": int(qty),
        "warn_cnfr_yn": "N",
    }
    response = await client.request(
        method="POST",
        path="/trading/v1/overseas/orders/cancel",
        data=payload,
    )
    data = response.get("data") or {}
    warn_code = str(data.get("warn_cls_code") or "0").strip()
    if warn_code in OVERSEAS_RESEND_WARN_CODES:
        payload["warn_cnfr_yn"] = "Y"
        response = await client.request(
            method="POST",
            path="/trading/v1/overseas/orders/cancel",
            data=payload,
        )
        data = response.get("data") or {}
        warn_code = str(data.get("warn_cls_code") or "0").strip()

    if warn_code != "0":
        warn_msg = str(data.get("warn_msg") or "").strip()
        raise MeritzApiError(
            rsp_cd="ORDER_CANCEL_NOT_ACCEPTED",
            message=f"취소 주문이 접수되지 않았습니다 (warn_cls_code={warn_code}): {warn_msg}",
            raw=response,
            warn_cls_code=warn_code,
        )

    odno = str(data.get("oder_no") or "")
    return {**data, "ODNO": odno, "odno": odno}


async def get_ccnl(
    client: AsyncMeritzClient,
    start_date: str = "",
    end_date: str = "",
    ovrs_excg_cd: str = "%",
) -> List[Dict[str, Any]]:
    """해외주식 당일 주문체결내역 조회 (`GET /trading/v1/overseas/orders/today`).

    reconciliation 모듈과 엔진 긴급정지(kill)에서 사용하는 표준 필드명으로 정규화하여 반환한다.
    """
    response = await client.request(
        method="GET",
        path="/trading/v1/overseas/orders/today",
    )
    rows = response.get("data") or []
    if not isinstance(rows, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = (r.get("ovrs_stck_symb_iscd") or r.get("pdno") or "").strip()
        vndr = str(r.get("ovrs_exch_vndr_idno") or "0537").strip()
        excg = VNDR_IDNO_TO_EXCG.get(vndr, r.get("ovrs_excg_cd") or "NASD")
        # 메리츠 seln_byov_clcd / deal_cls_code: "1"=매도, "2"=매수 -> reconciliation 표준 "01"(매도), "02"(매수)
        raw_side = str(r.get("seln_byov_clcd") or r.get("deal_cls_code") or "").strip()
        if raw_side in ("1", "01"):
            sll_buy_cd = "01"
        elif raw_side in ("2", "02"):
            sll_buy_cd = "02"
        else:
            sll_buy_cd = raw_side

        ord_tmd = str(r.get("oder_pros_time") or r.get("ord_tmd") or "").strip()
        if not ord_tmd and r.get("oder_exec_ymdh"):
            # "2026-08-26 12:48:23" -> "124823"
            parts = str(r["oder_exec_ymdh"]).split()
            if len(parts) == 2:
                ord_tmd = parts[1].replace(":", "")[:6]

        normalized.append(
            {
                **r,
                "odno": str(r.get("oder_no") if r.get("oder_no") is not None else r.get("odno", "")),
                "pdno": sym,
                "ovrs_excg_cd": excg,
                "sll_buy_dvsn_cd": sll_buy_cd,
                "ft_ord_qty": str(r.get("odqt") if r.get("odqt") is not None else r.get("ft_ord_qty", 0)),
                "ft_ord_unpr3": str(
                    r.get("ovrs_stck_oder_prc")
                    if r.get("ovrs_stck_oder_prc") is not None
                    else r.get("ft_ord_unpr3", 0)
                ),
                "ft_ccld_qty": str(r.get("cntg_qty") if r.get("cntg_qty") is not None else r.get("ft_ccld_qty", 0)),
                "ft_ccld_unpr3": str(
                    r.get("cntg_crnc_prc")
                    if r.get("cntg_crnc_prc") is not None
                    else r.get("ft_ccld_unpr3", 0)
                ),
                "nccs_qty": str(r.get("ncnt_qty") if r.get("ncnt_qty") is not None else r.get("nccs_qty", 0)),
                "ord_tmd": ord_tmd,
                "rjct_rson": str(r.get("ovrs_stck_rfus_reas") or r.get("rjct_rson") or ""),
            }
        )
    return normalized


async def get_fx_rate(client: AsyncMeritzClient, currency: str = "USD") -> float:
    """고시환율 조회 (`GET /forex/v1/rates`)."""
    response = await client.request(method="GET", path="/forex/v1/rates")
    rows = response.get("data") or []
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("crcd") == currency:
                return float(r.get("stnd_exrt") or r.get("exrt") or 0.0)
    elif isinstance(rows, dict):
        return float(rows.get("stnd_exrt") or rows.get("exrt") or 0.0)
    return 0.0

