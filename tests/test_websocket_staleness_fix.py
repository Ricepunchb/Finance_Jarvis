import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from core.config import settings
from core.risk import RiskManager
from core.websocket_client import KISWebSocketClient


def test_risk_is_ws_healthy_when_websocket_disabled():
    async def run():
        conn = AsyncMock()
        risk = RiskManager(conn)

        with patch.object(settings, "ENABLE_WEBSOCKET", False):
            healthy = await risk.is_ws_healthy()
            assert healthy is True
            conn.execute.assert_not_called()

    asyncio.run(run())


def test_risk_is_ws_healthy_when_websocket_enabled():
    async def run():
        conn = MagicMock()
        risk = RiskManager(conn)

        # 1. Stale timestamp
        stale_time = time.time() - 3600
        with patch("core.db.get_state", new=AsyncMock(return_value=str(stale_time))):
            with patch.object(settings, "ENABLE_WEBSOCKET", True):
                healthy = await risk.is_ws_healthy()
                assert healthy is False

        # 2. Fresh timestamp
        fresh_time = time.time() - 5
        with patch("core.db.get_state", new=AsyncMock(return_value=str(fresh_time))):
            with patch.object(settings, "ENABLE_WEBSOCKET", True):
                healthy = await risk.is_ws_healthy()
                assert healthy is True

    asyncio.run(run())


def test_ws_client_is_connected():
    from websockets.protocol import State

    client = KISWebSocketClient("test_id", AsyncMock(), AsyncMock())
    assert client.is_connected is False

    # Mock ws open
    mock_ws = MagicMock()
    mock_ws.state = State.OPEN
    client._ws = mock_ws
    assert client.is_connected is True

    # Mock ws closed
    mock_ws.state = State.CLOSED
    assert client.is_connected is False


def test_ws_client_pingpong_echo():
    from websockets.protocol import State

    async def run():
        client = KISWebSocketClient("test_id", AsyncMock(), AsyncMock())
        mock_ws = AsyncMock()
        mock_ws.state = State.OPEN
        client._ws = mock_ws

        ping_msg = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261026130000"}})
        await client._handle_message(ping_msg)

        # Must echo the raw string via send
        mock_ws.send.assert_awaited_once_with(ping_msg)

    asyncio.run(run())


def test_ws_client_run_loop_time_import_and_no_name_error():
    """_run 태스크가 실행될 때 time.time() NameError 없이 정상 구동되는지 검증."""
    from websockets.protocol import State

    async def run():
        on_any_mock = AsyncMock()
        client = KISWebSocketClient("test_id", AsyncMock(), on_any_mock)
        mock_ws = AsyncMock()
        mock_ws.state = State.OPEN

        # async iterator simulating receiving 1 message then closing
        async def mock_aiter():
            yield json.dumps({"header": {"tr_id": "PINGPONG"}})

        mock_ws.__aiter__ = lambda self: mock_aiter()
        client._ws = mock_ws

        task = asyncio.create_task(client._run())
        # Let task run briefly
        await asyncio.sleep(0.05)
        client._stopping = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert on_any_mock.await_count >= 1

    asyncio.run(run())

