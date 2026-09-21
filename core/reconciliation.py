# core/reconciliation.py
"""엔진 시작 시(크래시 재기동 포함) 반드시 먼저 실행해야 하는 대조 절차.

KIS 주문 API는 클라이언트가 지정하는 idempotency key를 지원하지 않는다 (확인됨).
따라서 "이 intent가 실제로 브로커에 도달했는가"는 이번 사이클의 일별체결내역과
symbol/side/qty/price/시간창으로 fuzzy-match 하는 방법밖에 없다. 후보가 정확히
하나면 그 주문으로 확정하고, 0개면 "도달 안 함"으로 안전하게 결론 내리지만,
2개 이상이면 절대 추측하지 않고 UNKNOWN으로 멈춰 사람이 확인하게 한다.
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db, kis_domestic, kis_overseas
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# intent 생성 시각과 KIS 체결내역의 주문시각(ord_tmd) 사이에 허용하는 오차.
MATCH_WINDOW_SEC = 180

_SIDE_TO_SLL_BUY_CD = {"sell": "01", "buy": "02"}

# 국내/해외 체결내역 조회 응답의 필드명이 달라서(ord_qty vs ft_ord_qty 등)
# market별로 어떤 필드를 볼지만 표로 관리하고, 매칭/상태판정 로직 자체는 공유한다.
_MARKET_FIELDS = {
    "domestic": {"qty": "ord_qty", "price": "ord_unpr", "ccld_qty": "tot_ccld_qty"},
    "overseas": {"qty": "ft_ord_qty", "price": "ft_ord_unpr3", "ccld_qty": "ft_ccld_qty"},
}


def _ord_tmd_to_epoch(ord_tmd: str, reference_epoch: float) -> Optional[float]:
    """'HHMMSS' 형태의 KIS 주문시각을 reference_epoch와 같은 날짜의 epoch로 변환."""
    if not ord_tmd or len(ord_tmd) < 6:
        return None
    ref_dt_kst = datetime.fromtimestamp(reference_epoch, tz=KST)
    try:
        h, m, s = int(ord_tmd[0:2]), int(ord_tmd[2:4]), int(ord_tmd[4:6])
    except ValueError:
        return None
    combined = ref_dt_kst.replace(hour=h, minute=m, second=s, microsecond=0)
    return combined.timestamp()


def _resolve_status(row: Dict[str, Any], market: str = "domestic") -> str:
    fields = _MARKET_FIELDS[market]
    ccld_qty = float(row.get(fields["ccld_qty"]) or 0)
    ord_qty = float(row.get(fields["qty"]) or 0)

    if ccld_qty >= ord_qty and ord_qty > 0:
        return "FILLED"

    if market == "domestic":
        rjct_qty = float(row.get("rjct_qty") or 0)
        cncl_yn = row.get("cncl_yn") == "Y"
        if cncl_yn and ccld_qty == 0:
            return "CANCELLED"
        if rjct_qty > 0 and ccld_qty == 0:
            return "REJECTED"
    else:
        # 해외 체결내역(inquire-ccnl)은 거부사유(rjct_rson)만 확인한다 — 정정취소구분(rvse_cncl_dvsn)의
        # 정확한 코드값 의미가 공식 문서에 명확히 나오지 않아 취소 자동판정은 v1에서 보류한다
        # (안전한 방향: 취소된 주문도 그냥 SUBMITTED로 남아 다음 재기동 때 다시 검토됨 - 중복주문으로는
        # 이어지지 않음).
        rjct_rson = (row.get("rjct_rson") or "").strip()
        if rjct_rson and rjct_rson != "0" and ccld_qty == 0:
            return "REJECTED"

    if ccld_qty > 0:
        return "PARTIALLY_FILLED"
    return "SUBMITTED"


async def _find_candidates(
    ccld_rows: List[Dict[str, Any]], intent: Dict[str, Any]
) -> List[Dict[str, Any]]:
    market = intent.get("market", "domestic")
    fields = _MARKET_FIELDS[market]
    side_cd = _SIDE_TO_SLL_BUY_CD.get(intent["side"])
    candidates = []
    for row in ccld_rows:
        if row.get("pdno") != intent["symbol"]:
            continue
        if row.get("sll_buy_dvsn_cd") != side_cd:
            continue
        if float(row.get(fields["qty"]) or -1) != float(intent["qty"]):
            continue
        if intent["order_type"] == "limit":
            if float(row.get(fields["price"]) or -1) != float(intent["price"] or -1):
                continue
        ord_epoch = _ord_tmd_to_epoch(row.get("ord_tmd", ""), intent["created_at"])
        if ord_epoch is None:
            continue
        if abs(ord_epoch - intent["created_at"]) > MATCH_WINDOW_SEC:
            continue
        candidates.append(row)
    return candidates


async def reconcile_positions(client: AsyncKISClient, conn: aiosqlite.Connection) -> None:
    """KIS 잔고조회 결과를 ground truth로 삼아 positions 테이블을 강제 재동기화한다 (국내+해외)."""
    balance = await kis_domestic.get_balance(client)
    for holding in balance["holdings"]:
        symbol = holding.get("pdno")
        qty = float(holding.get("hldg_qty") or 0)
        avg_price = float(holding.get("pchs_avg_pric") or 0)
        if symbol:
            await db.upsert_position(conn, symbol, qty, avg_price)

    try:
        present = await kis_overseas.get_present_balance_krw(client)
    except Exception:
        # 해외 잔고 조회가 실패해도(예: 보유 종목이 없어 계좌 자체가 해외 서비스 미이용) 국내
        # positions 재동기화는 이미 끝났으니 여기서 조용히 넘어간다 - 다음 재기동 때 다시 시도.
        return
    for holding in present["holdings"]:
        symbol = holding.get("pdno")
        qty = float(holding.get("cblc_qty13") or 0)
        avg_price = float(holding.get("avg_unpr3") or 0)
        currency = holding.get("crcy_cd") or "USD"
        if symbol:
            await db.upsert_position(conn, symbol, qty, avg_price, currency=currency)


async def reconcile_unresolved_intents(client: AsyncKISClient, conn: aiosqlite.Connection) -> None:
    """재기동 시 PENDING/SUBMITTED 상태로 남아있는 모든 intent를 KIS 실제 기록과 대조한다.

    이 함수가 끝나기 전까지는 엔진이 신규 주문을 절대 내지 않아야 한다.
    """
    unresolved = await db.get_unresolved_intents(conn)
    if not unresolved:
        return

    today = datetime.now(tz=KST).strftime("%Y%m%d")
    domestic_rows = await kis_domestic.get_daily_ccld(client, start_date=today, end_date=today)
    try:
        overseas_rows = await kis_overseas.get_ccnl(client, start_date=today, end_date=today)
    except Exception:
        logger.warning("해외 체결내역 조회 실패 - 국내 intent만 대조하고 해외는 다음 재기동에 재시도")
        overseas_rows = []

    for intent in unresolved:
        rows = overseas_rows if intent.get("market") == "overseas" else domestic_rows
        candidates = await _find_candidates(rows, intent)
        if len(candidates) == 0:
            # KIS 기록에 없음 -> 브로커에 도달하지 못했다고 결론. 재시도는 다음 사이클에 새 intent로.
            await db.update_order_intent(conn, intent["intent_id"], status="NOT_SUBMITTED")
        elif len(candidates) == 1:
            row = candidates[0]
            status = _resolve_status(row, market=intent.get("market", "domestic"))
            await db.update_order_intent(
                conn, intent["intent_id"], status=status, kis_order_no=row.get("odno")
            )
        else:
            # 후보가 여러 개면 추측하지 않는다 — 사람이 확인할 때까지 이 종목은 매매 금지 대상.
            await db.update_order_intent(conn, intent["intent_id"], status="UNKNOWN")


async def run_startup_reconciliation(client: AsyncKISClient, conn: aiosqlite.Connection) -> None:
    await reconcile_unresolved_intents(client, conn)
    await reconcile_positions(client, conn)
    await db.set_state(conn, "last_reconciled_at", str(time.time()))
