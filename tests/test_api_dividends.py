# tests/test_api_dividends.py
import pytest
from fastapi.testclient import TestClient
from api.main import app
from core import db
import aiosqlite

@pytest.fixture(autouse=True)
def init_test_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_api_div.db"

    async def _mock_get_conn():
        c = await aiosqlite.connect(str(db_file))
        c.row_factory = aiosqlite.Row
        await c.executescript(db.SCHEMA)
        return c

    monkeypatch.setattr(db, "get_connection", _mock_get_conn)


def test_api_dividend_crud():
    client = TestClient(app)

    # 1. POST /dividends
    payload = {
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
        "notes": "삼성전자 분기배당",
    }
    res = client.post("/dividends", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    div_id = data["id"]
    assert div_id > 0

    # 2. GET /dividends
    res = client.get("/dividends")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["net_amount_krw"] == 30550.0

    # 3. GET /dividends/{id}
    res = client.get(f"/dividends/{div_id}")
    assert res.status_code == 200
    assert res.json()["symbol"] == "005930"

    # 4. GET /dividends/summary
    res = client.get("/dividends/summary")
    assert res.status_code == 200
    summary = res.json()
    assert len(summary["by_symbol"]) == 1
    assert summary["by_symbol"][0]["symbol"] == "005930"
    assert summary["totals"]["total_net_krw"] == 30550.0

    # 5. PUT /dividends/{id}
    res = client.put(f"/dividends/{div_id}", json={"notes": "수정된 메모"})
    assert res.status_code == 200
    res = client.get(f"/dividends/{div_id}")
    assert res.json()["notes"] == "수정된 메모"

    # 6. DELETE /dividends/{id}
    res = client.delete(f"/dividends/{div_id}")
    assert res.status_code == 200
    res = client.get(f"/dividends/{div_id}")
    assert res.status_code == 404


def test_api_dividend_import_csv():
    client = TestClient(app)

    csv_data = """symbol,payment_date,net_amount,dividend_type,notes
005930,2026-04-15,30550,CASH,테스트1
069500,2026-05-04,15000,ETF_DIST,테스트2
"""
    res = client.post(
        "/dividends/import-csv",
        content=csv_data.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["inserted_count"] == 2

    # 확인
    res = client.get("/dividends")
    assert len(res.json()) == 2
