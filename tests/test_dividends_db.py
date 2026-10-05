# tests/test_dividends_db.py
import pytest
import aiosqlite
from core import db

@pytest.fixture
async def conn():
    c = await aiosqlite.connect(":memory:")
    c.row_factory = aiosqlite.Row
    await c.executescript(db.SCHEMA)
    yield c
    await c.close()

@pytest.mark.anyio
async def test_dividend_crud(conn):
    # 1. Create
    data = {
        "symbol": "005930",
        "market": "domestic",
        "dividend_type": "CASH",
        "record_date": "2026-03-31",
        "payment_date": "2026-04-15",
        "qty": 100.0,
        "dps": 361.0,
        "gross_amount": 36100.0,
        "tax_amount": 5550.0,
        "net_amount": 30550.0,
        "currency": "KRW",
        "fx_rate": 1.0,
        "net_amount_krw": 30550.0,
        "source": "MANUAL",
        "notes": "2026 Q1 배당",
    }
    div_id = await db.add_dividend(conn, data)
    assert div_id > 0

    # 2. Read single
    row = await db.get_dividend(conn, div_id)
    assert row is not None
    assert row["symbol"] == "005930"
    assert row["net_amount_krw"] == 30550.0
    assert row["notes"] == "2026 Q1 배당"

    # 3. List
    rows = await db.list_dividends(conn)
    assert len(rows) == 1

    # 4. Update
    await db.update_dividend(conn, div_id, {"net_amount": 32000.0, "net_amount_krw": 32000.0, "notes": "수정됨"})
    updated = await db.get_dividend(conn, div_id)
    assert updated["net_amount_krw"] == 32000.0
    assert updated["notes"] == "수정됨"

    # 5. Delete
    deleted = await db.delete_dividend(conn, div_id)
    assert deleted is True
    assert await db.get_dividend(conn, div_id) is None

@pytest.mark.anyio
async def test_dividend_summaries_and_kis_upsert(conn):
    # KIS 배당 2건 upsert
    kis_items = [
        {
            "symbol": "005930",
            "market": "domestic",
            "dividend_type": "CASH",
            "record_date": "2026-03-31",
            "payment_date": "2026-04-15",
            "qty": 50.0,
            "dps": 361.0,
            "gross_amount": 18050.0,
            "tax_amount": 2770.0,
            "net_amount": 15280.0,
            "currency": "KRW",
            "fx_rate": 1.0,
            "net_amount_krw": 15280.0,
            "source": "AUTO_KIS",
            "kis_mgmt_no": "KIS-20260415-001",
        },
        {
            "symbol": "069500",
            "market": "domestic",
            "dividend_type": "ETF_DIST",
            "record_date": "2026-04-30",
            "payment_date": "2026-05-04",
            "qty": 200.0,
            "dps": 120.0,
            "gross_amount": 24000.0,
            "tax_amount": 3690.0,
            "net_amount": 20310.0,
            "currency": "KRW",
            "fx_rate": 1.0,
            "net_amount_krw": 20310.0,
            "source": "AUTO_KIS",
            "kis_mgmt_no": "KIS-20260504-002",
        },
    ]

    inserted, skipped = await db.upsert_kis_dividends(conn, kis_items)
    assert inserted == 2
    assert skipped == 0

    # 중복 upsert 테스트 (0건 추가, 2건 스킵되어야 함)
    ins2, skip2 = await db.upsert_kis_dividends(conn, kis_items)
    assert ins2 == 0
    assert skip2 == 2

    # 종목별 요약
    by_sym = await db.get_dividend_summary_by_symbol(conn)
    assert len(by_sym) == 2
    assert by_sym["005930"]["count"] == 1
    assert by_sym["005930"]["total_net_krw"] == 15280.0
    assert by_sym["069500"]["total_net_krw"] == 20310.0

    # 월별 요약
    by_month = await db.get_monthly_dividends(conn)
    assert len(by_month) == 2
    month_map = {r["month"]: r for r in by_month}
    assert "2026-04" in month_map
    assert month_map["2026-04"]["total_net_krw"] == 15280.0
    assert "2026-05" in month_map
    assert month_map["2026-05"]["total_net_krw"] == 20310.0
