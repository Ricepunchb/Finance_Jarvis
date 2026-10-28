# core/websocket_client.py
"""KIS 실시간 체결통보 웹소켓 클라이언트. 국내(H0STCNI0/9)와 해외(H0GSCNI0/9)를
같은 연결에서 동시에 구독한다.

체결통보 데이터는 최초 구독 응답(system frame)에 담긴 key/iv로 AES-CBC 복호화해야
읽을 수 있다 — 국내/해외 tr_id마다 별도로 발급되므로 tr_id별로 키/iv를 따로 보관한다.
이 연결이 끊기면 우리는 주문이 실제로 체결됐는지 알 방법이 없어지므로, 매 수신(데이터든
PINGPONG이든)마다 RiskManager에 "살아있다"를 기록해 staleness 게이트가 이를 근거로
신규주문을 차단할 수 있게 한다.

국내/해외 체결통보의 컬럼 레이아웃은 다르지만, engine.py가 실제로 읽는 필드
(ODER_NO, CNTG_YN, RFUS_YN, CNTG_QTY, CNTG_UNPR)는 이름이 동일해서 별도 정규화가
필요 없다.
"""
import asyncio
import json
import logging
import time
from base64 import b64decode
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp
import websockets
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from core.config import settings
from core.tr_ids import ccnl_notice_overseas_tr_id, ccnl_notice_tr_id

logger = logging.getLogger(__name__)

WS_URL = (
    "ws://ops.koreainvestment.com:31000/tryitout"
    if settings.IS_MOCK
    else "ws://ops.koreainvestment.com:21000/tryitout"
)

_DOMESTIC_CCNL_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND", "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR",
    "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY",
    "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN", "FILLER", "CRDT_CLS",
    "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC",
]

_OVERSEAS_CCNL_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND2", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR", "STCK_CNTG_HOUR",
    "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY", "ACNT_NAME",
    "CNTG_ISNM", "ODER_COND", "DEBT_GB", "DEBT_DATE", "START_TM", "END_TM",
    "TM_DIV_TP", "CNTG_UNPR12",
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
    """실시간 체결통보(국내 H0STCNI0/9 + 해외 H0GSCNI0/9) 구독. 종료 시 stop()을 명시적으로 호출.

    연결이 끊기면 지수 백오프로 자동 재연결한다 — 이 연결이 죽으면 risk.is_ws_healthy()가
    staleness 게이트를 걸어 신규주문뿐 아니라 손절/트레일링익절 평가까지 멈추므로, 사람이
    엔진을 수동 재시작하기 전까지 방치되면 안 된다.
    """

    _RECONNECT_BASE_DELAY_SEC = 2
    _RECONNECT_MAX_DELAY_SEC = 60
    _HEARTBEAT_INTERVAL_SEC = 30  # 연결 유지 시 생존신호 갱신 주기 (데드락 방지)

    def __init__(self, hts_id: str, on_fill: OnFill, on_any_message: OnAnyMessage, include_overseas: bool = False):
        self.hts_id = hts_id
        self.on_fill = on_fill
        self.on_any_message = on_any_message
        self.include_overseas = include_overseas
        self._ws: Optional[websockets.ClientConnection] = None
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._columns_by_tr_id: Dict[str, List[str]] = {}
        self._enc_state: Dict[str, Tuple[str, str]] = {}  # tr_id -> (key, iv)
        self._stopping = False

    @property
    def is_connected(self) -> bool:
        """웹소켓 소켓 객체가 정상적으로 연결(Open)되어 있는지 여부."""
        if self._ws is None:
            return False
        state = getattr(self._ws, "state", None)
        if state is not None:
            return getattr(state, "name", str(state)) == "OPEN"
        return not getattr(self._ws, "closed", True)

    async def start(self) -> None:
        self._stopping = False
        # 첫 연결 시도 — 모의투자 서버의 일시적 핸드셰이크 거부 시에도 최대 3회 즉시 재시도
        for attempt in range(1, 4):
            try:
                await self._connect_and_subscribe()
                logger.info("KIS 실시간 체결통보 웹소켓 연결 성공")
                break
            except Exception as e:
                logger.warning(f"KIS 웹소켓 초기 연결 시도 {attempt}/3 실패: {e}")
                if attempt < 3:
                    await asyncio.sleep(1.0)
        # 초기 연결이 일시 실패하더라도 _run 태스크가 지수 백오프로 재연결을 지속 시도함
        self._task = asyncio.create_task(self._run())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """연결 유지 중 주기적 생존신호 갱신 — 체결이 없는 장중 유휴 상태에서 staleness 데드락 방지."""
        while not self._stopping:
            try:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_SEC)
                if self.is_connected:
                    await self.on_any_message()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"웹소켓 하트비트 갱신 중 예외: {e}")

    async def _connect_and_subscribe(self) -> None:
        approval_key = await issue_approval_key()
        # KIS 웹소켓은 표준 WS 핑퐁 대신 자체 전문을 사용하므로, 클라이언트 측 주기적 ping_interval을 비활성화한다.
        self._ws = await websockets.connect(WS_URL, ping_interval=None, ping_timeout=None)
        self._enc_state = {}  # 이전 세션의 key/iv는 무효 — 구독 응답으로 새로 받는다

        subscriptions = [(ccnl_notice_tr_id(), _DOMESTIC_CCNL_COLUMNS)]
        if self.include_overseas:
            subscriptions.append((ccnl_notice_overseas_tr_id(), _OVERSEAS_CCNL_COLUMNS))

        for tr_id, columns in subscriptions:
            self._columns_by_tr_id[tr_id] = columns
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
        # 구독 요청 전송 완료 즉시 생존 신호 기록
        await self.on_any_message()

    async def _run(self) -> None:
        delay = self._RECONNECT_BASE_DELAY_SEC
        while not self._stopping:
            try:
                if self._ws is not None:
                    connected_at = time.time()
                    try:
                        async for raw in self._ws:
                            await self.on_any_message()  # 데이터든 PINGPONG이든 "살아있다"의 근거
                            await self._handle_message(raw)
                            # 최소 30초 이상 연결이 안정적으로 유지되었을 때만 백오프 딜레이를 초기화
                            if time.time() - connected_at > 30.0:
                                delay = self._RECONNECT_BASE_DELAY_SEC
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        logger.warning(f"KIS 웹소켓 연결이 종료되었습니다 ({type(e).__name__}: {e})")
                    finally:
                        if self._ws is not None:
                            try:
                                await self._ws.close()
                            except Exception:
                                pass
                            self._ws = None

                if self._stopping:
                    return

                logger.warning(f"{delay}초 후 KIS 웹소켓 재연결을 시도합니다")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                delay = min(delay * 2, self._RECONNECT_MAX_DELAY_SEC)

                try:
                    await self._connect_and_subscribe()
                    logger.info("KIS 웹소켓 재연결 성공")
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning(f"KIS 웹소켓 재연결 시도 실패 ({type(e).__name__}: {e})")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("KIS 웹소켓 루프 내 처리되지 않은 예외 발생 — 지연 후 재연결 시도")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return

    async def _handle_message(self, raw: str) -> None:
        if raw[0] in ("0", "1"):
            parts = raw.split("|")
            if len(parts) < 4:
                return
            tr_id = parts[1]
            columns = self._columns_by_tr_id.get(tr_id)
            if columns is None:
                return  # 구독하지 않은 tr_id — 무시
            payload = parts[3]
            enc = self._enc_state.get(tr_id)
            if enc is not None:
                payload = _aes_cbc_base64_dec(enc[0], enc[1], payload)
            row = dict(zip(columns, payload.split("^")))
            await self.on_fill(row)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        header = msg.get("header", {})
        if header.get("tr_id") == "PINGPONG":
            if self.is_connected:
                # KIS 웹소켓 서버는 수신한 PINGPONG 텍스트 전문 그대로 Echo 응답(send)을 요구한다
                await self._ws.send(raw)
            return

        body = msg.get("body", {})
        output = body.get("output", {})
        if header.get("encrypt") == "Y" and "key" in output and "iv" in output:
            self._enc_state[header.get("tr_id")] = (output["key"], output["iv"])

    async def stop(self) -> None:
        self._stopping = True  # 진행 중이던 재연결 시도가 close() 이후에 새 연결을 여는 것을 막는다
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
