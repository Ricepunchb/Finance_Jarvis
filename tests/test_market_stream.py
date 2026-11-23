# tests/test_market_stream.py
"""실시간 웹소켓 1분 롤링 집계(MinuteFlowAggregator) 및 멀티 TR 라우팅 테스트."""
import asyncio
from unittest.mock import AsyncMock

from core.market_stream import MinuteFlowAggregator
from core.tr_ids import WS_EXECUTION_TR_ID, WS_PROGRAM_TR_ID
from core.websocket_client import KISWebSocketClient, _DOMESTIC_EXECUTION_COLUMNS, _DOMESTIC_PROGRAM_COLUMNS


def test_minute_aggregator_detects_surge_with_debounce():
    agg = MinuteFlowAggregator(
        surge_pct=0.015,
        plunge_pct=-0.015,
        rvol_mult=3.0,
        cttr_bull=120.0,
        debounce_sec=180,
    )
    base_ts = 1_700_000_000.0
    # 5분간 평온한 체결 (분당 100주, 가격 10,000원)
    for m in range(5):
        ev = agg.on_tick("005930", 10000.0, 100.0, cttr=100.0, side="buy", ts=base_ts + m * 60)
        assert ev is None

    # 6분째: 가격 +2% 급등 + 거래량 4배(400주) + 체결강도 135
    ev1 = agg.on_tick("005930", 10200.0, 400.0, cttr=135.0, side="buy", ts=base_ts + 5 * 60)
    assert ev1 is not None
    assert ev1.kind == "SURGE"
    assert ev1.symbol == "005930"
    assert ev1.rvol >= 3.0

    # 디바운스 구간(180초 이내) 재발화 억제
    ev2 = agg.on_tick("005930", 10250.0, 100.0, cttr=140.0, side="buy", ts=base_ts + 5 * 60 + 30)
    assert ev2 is None


def test_minute_aggregator_detects_plunge_and_program_dump():
    agg = MinuteFlowAggregator(
        plunge_pct=-0.015,
        rvol_mult=3.0,
        cttr_bear=80.0,
        debounce_sec=180,
    )
    base_ts = 1_700_000_000.0
    for m in range(3):
        agg.on_tick("034230", 11000.0, 100.0, cttr=100.0, side="buy", ts=base_ts + m * 60)
        agg.on_program("034230", cum_net_vol=0.0, ts=base_ts + m * 60)

    # 급락 + 프로그램 순매도 전환
    agg.on_program("034230", cum_net_vol=-5000.0, ts=base_ts + 3 * 60)
    ev = agg.on_tick("034230", 10750.0, 350.0, cttr=72.0, side="sell", ts=base_ts + 3 * 60 + 10)
    assert ev is not None
    assert ev.kind == "PLUNGE"
    assert ev.return_pct <= -0.015

    snap = agg.get_snapshot("034230")
    assert snap is not None
    assert snap["cttr"] == 72.0


def test_kis_ws_routes_execution_and_program_frames():
    on_fill = AsyncMock()
    on_tick = AsyncMock()
    on_program = AsyncMock()
    on_any = AsyncMock()

    ws = KISWebSocketClient(
        hts_id="test_hts",
        on_fill=on_fill,
        on_any_message=on_any,
        on_tick=on_tick,
        on_program=on_program,
    )
    ws._columns_by_tr_id[WS_EXECUTION_TR_ID] = _DOMESTIC_EXECUTION_COLUMNS
    ws._columns_by_tr_id[ws._program_tr_id] = _DOMESTIC_PROGRAM_COLUMNS

    exec_fields = ["005930", "103000", "75000"] + ["0"] * (len(_DOMESTIC_EXECUTION_COLUMNS) - 3)
    raw_exec = f"0|{WS_EXECUTION_TR_ID}|001|{'^'.join(exec_fields)}"

    prog_fields = ["005930", "103000", "100", "1000", "500", "5000", "400", "4000", "0", "0", "400"]
    raw_prog = f"0|{ws._program_tr_id}|001|{'^'.join(prog_fields)}"

    async def run():
        await ws._handle_message(raw_exec)
        await ws._handle_message(raw_prog)

    asyncio.run(run())

    on_tick.assert_awaited_once()
    assert on_tick.await_args.args[0]["MKSC_SHRN_ISCD"] == "005930"
    assert on_tick.await_args.args[0]["STCK_PRPR"] == "75000"

    on_program.assert_awaited_once()
    assert on_program.await_args.args[0]["NTBY_CNQN"] == "400"
    on_fill.assert_not_awaited()
