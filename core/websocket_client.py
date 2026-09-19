# core/websocket_client.py
"""KIS 실시간 체결통보 웹소켓 클라이언트 (국내주식, H0STCNI0/H0STCNI9).

체결통보 데이터는 최초 구독 응답(system frame)에 담긴 key/iv로 AES-CBC 복호화해야
읽을 수 있다. 이 연결이 끊기면 우리는 주문이 실제로 체결됐는지 알 방법이 없어지므로,
매 수신(데이터든 PINGPONG이든)마다 RiskManager에 "살아있다"를 기록해 staleness 게이트가
이를 근거로 신규주문을 차단할 수 있게 한다.
"""
import asyncio
import json
import logging
from base64 import b64decode
from typing import Awaitable, Callable, Optional

import aiohttp
import websockets
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from core.config import settings
from core.tr_ids import ccnl_notice_tr_id

logger = logging.getLogger(__name__)

WS_URL = "ws://ops.koreainvestment.com:31000" if settings.IS_MOCK else "ws://ops.koreainvestment.com:21000"

_CCNL_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND", "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR",
    "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY",
    "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN", "FILLER", "CRDT_CLS",
    "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC",
]

OnFill = Callable[[dict], Awaitable[None]]
OnAnyMessage = Callable[[], Awaitable[None]]


async def issue_approval_key() -> str:
    url = f"{settings.kis_domain}/oauth2/Approval"
    payload = {
        "grant_type": "client_credentials",
        "appkey": settings.KIS_APP_KEY,
        "secretkey": settings.KIS_APP_SECRET,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            data = await response.json()
            if response.status != 200:
                raise Exception(f"웹소켓 approval_key 발급 실패: {data}")
            return data["approval_key"]


def _aes_cbc_base64_dec(key: str, iv: str, cipher_text: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return bytes.decode(unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size))


class KISWebSocketClient:
    """국내주식 실시간 체결통보(H0STCNI0/9) 구독 전용. 종료 시 disconnect()를 명시적으로 호출."""

    def __init__(self, hts_id: str, on_fill: OnFill, on_any_message: OnAnyMessage):
        self.hts_id = hts_id
        self.on_fill = on_fill
        self.on_any_message = on_any_message
        self._ws: Optional[websockets.ClientConnection] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._enc_key: Optional[str] = None
        self._enc_iv: Optional[str] = None

    async def start(self) -> None:
        approval_key = await issue_approval_key()
        self._ws = await websockets.connect(WS_URL)
        tr_id = ccnl_notice_tr_id()
        subscribe_msg = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": self.hts_id}},
        }
        await self._ws.send(json.dumps(subscribe_msg))
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                await self.on_any_message()  # 데이터든 PINGPONG이든 "살아있다"의 근거
                await self._handle_message(raw)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            logger.warning("KIS 웹소켓 연결이 종료되었습니다.")
        except Exception:
            logger.exception("KIS 웹소켓 처리 중 예외 발생")

    async def _handle_message(self, raw: str) -> None:
        if raw[0] in ("0", "1"):
            parts = raw.split("|")
            if len(parts) < 4:
                return
            payload = parts[3]
            if self._enc_key and self._enc_iv:
                payload = _aes_cbc_base64_dec(self._enc_key, self._enc_iv, payload)
            row = dict(zip(_CCNL_COLUMNS, payload.split("^")))
            await self.on_fill(row)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        header = msg.get("header", {})
        if header.get("tr_id") == "PINGPONG":
            if self._ws is not None:
                await self._ws.pong(raw)
            return

        body = msg.get("body", {})
        output = body.get("output", {})
        if header.get("encrypt") == "Y" and "key" in output and "iv" in output:
            self._enc_key = output["key"]
            self._enc_iv = output["iv"]

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._ws is not None:
            await self._ws.close()
