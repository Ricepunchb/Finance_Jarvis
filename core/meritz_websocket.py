# core/meritz_websocket.py
"""메리츠증권 실시간 해외주식 주문 접수·체결 통보 웹소켓 클라이언트 (`ohhts_cntg_exst`).

메리츠 실시간 웹소켓의 특징:
1. 단일 접속점(`wss://openapi.imeritz.com:29443/websocket`)을 사용한다.
2. 별도 approval_key 발급 없이 REST OAuth 접근토큰(`Bearer {token}`)을 구독 메시지 헤더에 담아 인증한다.
3. 구독 응답(ACK)은 `header.rsp_cd`가 `"00000"` 또는 `"0000"`으로 내려온다.
4. `ohhts_cntg_exst`는 계좌 단위 통보이므로 `tr_key` 없이 한 번만 구독하면 모든 해외 종목 체결이 수신된다.
5. 수신된 체결 데이터를 엔진의 `_on_fill(row)` 콜백 포맷(`ODER_NO`, `CNTG_YN`, `RFUS_YN`, `CNTG_QTY`, `CNTG_UNPR`)으로
   정규화하여 전달한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets

from core.config import settings
from core.meritz_client import AsyncMeritzClient

logger = logging.getLogger(__name__)

TR_CD_OVERSEAS_NOTICE = "ohhts_cntg_exst"

OnFill = Callable[[dict], Awaitable[None]]
OnAnyMessage = Callable[[], Awaitable[None]]


def normalize_meritz_notice(body: dict) -> dict:
    """메리츠 `ohhts_cntg_exst` body를 엔진 `_on_fill` 표준 딕셔너리로 변환한다."""
    oder_no = str(body.get("oder_no") or "").strip()
    cntg_qty_str = str(body.get("cntg_qty") or "0").strip()
    cntg_prc_str = str(body.get("cntg_prc") or "0").strip()
    rfus_reas = str(body.get("rfus_reas") or body.get("aspr_rfus_reas_code") or "").strip()
    svc_id = str(body.get("svc_id") or "").strip()

    try:
        cntg_qty = float(cntg_qty_str)
    except ValueError:
        cntg_qty = 0.0

    # 거부 사유가 존재하면 거부(RFUS_YN="Y")
    rfus_yn = "Y" if rfus_reas else "N"
    # OVSMEM20007(체결) 또는 체결수량 > 0 이면 실제 체결("2"), 그 외 접수("1")
    cntg_yn = "2" if (svc_id == "OVSMEM20007" or cntg_qty > 0) else "1"

    return {
        "ODER_NO": oder_no,
        "CNTG_YN": cntg_yn,
        "RFUS_YN": rfus_yn,
        "CNTG_QTY": cntg_qty_str,
        "CNTG_UNPR": cntg_prc_str,
        "STCK_SHRN_ISCD": str(body.get("symb") or body.get("iscd") or "").strip(),
        "raw": body,
    }


class MeritzWebSocketClient:
    """메리츠 실시간 해외주식 주문 접수·체결 통보 구독 클라이언트."""

    _RECONNECT_BASE_DELAY_SEC = 2
    _RECONNECT_MAX_DELAY_SEC = 60
    _HEARTBEAT_INTERVAL_SEC = 30

    def __init__(
        self,
        client: AsyncMeritzClient,
        on_fill: OnFill,
        on_any_message: OnAnyMessage,
        ws_url: Optional[str] = None,
    ):
        self.client = client
        self.on_fill = on_fill
        self.on_any_message = on_any_message
        self.ws_url = ws_url or settings.MERITZ_WS_URL
        self._ws: Optional[websockets.ClientConnection] = None
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stopping = False

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        state = getattr(self._ws, "state", None)
        if state is not None:
            return getattr(state, "name", str(state)) == "OPEN"
        return not getattr(self._ws, "closed", True)

    async def start(self) -> None:
        self._stopping = False
        for attempt in range(1, 4):
            try:
                await self._connect_and_subscribe()
                logger.info("메리츠 실시간 해외 체결통보 웹소켓 연결 성공")
                break
            except Exception as e:
                logger.warning(f"메리츠 웹소켓 초기 연결 시도 {attempt}/3 실패: {e}")
                if attempt < 3:
                    await asyncio.sleep(1.0)
        self._task = asyncio.create_task(self._run())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_SEC)
                if self.is_connected:
                    await self.on_any_message()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"메리츠 웹소켓 하트비트 갱신 중 예외: {e}")

    async def _connect_and_subscribe(self) -> None:
        token = await self.client.issue_token()
        self._ws = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20)
        subscribe_msg = {
            "header": {
                "token": f"Bearer {token}",
                "tr_type": "1",
            },
            "body": {
                "tr_cd": TR_CD_OVERSEAS_NOTICE,
            },
        }
        await self._ws.send(json.dumps(subscribe_msg))
        await self.on_any_message()

    async def _run(self) -> None:
        delay = self._RECONNECT_BASE_DELAY_SEC
        while not self._stopping:
            try:
                if self._ws is None:
                    await self._connect_and_subscribe()
                async for raw in self._ws:
                    await self.on_any_message()
                    await self._handle_message(raw)
                delay = self._RECONNECT_BASE_DELAY_SEC
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._stopping:
                    break
                logger.warning(f"메리츠 웹소켓 연결 끊김 ({e}) — {delay}초 후 재연결")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._RECONNECT_MAX_DELAY_SEC)
            finally:
                self._ws = None

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        head = msg.get("header") or {}
        if "rsp_cd" in head:
            rsp_cd = str(head.get("rsp_cd") or "")
            if rsp_cd not in ("00000", "0000"):
                logger.error(f"메리츠 웹소켓 구독 실패: [{rsp_cd}] {head.get('rsp_msg')}")
            else:
                logger.info(f"메리츠 웹소켓 구독 ACK 확인 ({head.get('tr_cd')})")
            return

        body = msg.get("body")
        if isinstance(body, dict) and head.get("tr_cd") == TR_CD_OVERSEAS_NOTICE:
            normalized = normalize_meritz_notice(body)
            if normalized.get("ODER_NO"):
                await self.on_fill(normalized)

    async def stop(self) -> None:
        self._stopping = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._ws = None
        self._task = None
        self._heartbeat_task = None

