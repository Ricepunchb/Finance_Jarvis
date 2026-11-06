import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.meritz_websocket import (
    TR_CD_OVERSEAS_NOTICE,
    MeritzWebSocketClient,
    normalize_meritz_notice,
)


def test_normalize_meritz_notice():
    # 1. 체결 이벤트 (OVSMEM20007)
    body_fill = {
        "oder_no": "ORD12345",
        "cntg_qty": "10",
        "cntg_prc": "250.0",
        "svc_id": "OVSMEM20007",
        "symb": "AAPL",
    }
    normalized = normalize_meritz_notice(body_fill)
    assert normalized["ODER_NO"] == "ORD12345"
    assert normalized["CNTG_YN"] == "2"
    assert normalized["RFUS_YN"] == "N"
    assert normalized["CNTG_QTY"] == "10"
    assert normalized["CNTG_UNPR"] == "250.0"
    assert normalized["STCK_SHRN_ISCD"] == "AAPL"

    # 2. 거절 이벤트
    body_reject = {
        "oder_no": "ORD67890",
        "cntg_qty": "0",
        "rfus_reas": "주문가능금액 초과",
        "symb": "TSLA",
    }
    norm_reject = normalize_meritz_notice(body_reject)
    assert norm_reject["ODER_NO"] == "ORD67890"
    assert norm_reject["RFUS_YN"] == "Y"
    assert norm_reject["CNTG_YN"] == "1"


def test_websocket_message_handling():
    async def run():
        mock_client = AsyncMock()
        mock_client.issue_token.return_value = "MOCK_TOKEN"
        on_fill = AsyncMock()
        on_any_message = AsyncMock()

        ws_client = MeritzWebSocketClient(
            client=mock_client,
            on_fill=on_fill,
            on_any_message=on_any_message,
        )

        # ACK 메시지 처리
        ack_msg = json.dumps({
            "header": {
                "tr_cd": TR_CD_OVERSEAS_NOTICE,
                "rsp_cd": "00000",
                "rsp_msg": "성공",
            }
        })
        await ws_client._handle_message(ack_msg)
        assert not on_fill.called

        # 체결 메시지 처리
        fill_msg = json.dumps({
            "header": {
                "tr_cd": TR_CD_OVERSEAS_NOTICE,
            },
            "body": {
                "oder_no": "ORD9999",
                "cntg_qty": "5",
                "cntg_prc": "150.0",
                "svc_id": "OVSMEM20007",
                "symb": "MSFT",
            },
        })
        await ws_client._handle_message(fill_msg)
        assert on_fill.called
        call_row = on_fill.call_args[0][0]
        assert call_row["ODER_NO"] == "ORD9999"
        assert call_row["CNTG_YN"] == "2"
        assert call_row["CNTG_QTY"] == "5"
    asyncio.run(run())

