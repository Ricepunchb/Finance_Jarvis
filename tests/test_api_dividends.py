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


def test_api_dividend_sync(monkeypatch):
    from core import kis_domestic

    mock_rows = [
        {
            "acno10": "1234567801",
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "rght_type_cd": "02",
            "bass_dt": "20251231",
            "cash_dfrm_dt": "20260415",
            "cblc_qty": "100",
            "last_alct_amt": "36100",
            "tax_amt": "5550",
            "sbsc_unpr": "361",
        },
        {
            "acno10": "1234567801",
            "pdno": "069500",
            "prdt_name": "KODEX 200",
            "rght_type_cd": "03",
            "bass_dt": "20260131",
            "cash_dfrm_dt": "20260205",
            "cblc_qty": "50",
            "last_alct_amt": "15000",
            "tax_amt": "2310",
            "sbsc_unpr": "300",
        },
    ]

    async def _mock_get_period_rights(*args, **kwargs):
        return mock_rows

    monkeypatch.setattr(kis_domestic, "get_period_rights", _mock_get_period_rights)

    client = TestClient(app)

    # 1. 쿼리 파라미터로 동기화 호출
    res = client.post("/dividends/sync?start_date=20250101&end_date=20261006")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["total_fetched"] == 2
    assert data["inserted_count"] == 2
    assert data["skipped_count"] == 0

    # 2. 동일 데이터 재동기화 시 중복 스킵 확인
    res2 = client.post("/dividends/sync", params={"start_date": "20250101", "end_date": "20261006"})
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["total_fetched"] == 2
    assert data2["inserted_count"] == 0
    assert data2["skipped_count"] == 2

    # DB에 저장된 내용 검증
    res_list = client.get("/dividends")
    rows = res_list.json()
    assert len(rows) == 2
    samsung = next(r for r in rows if r["symbol"] == "005930")
    assert samsung["dividend_type"] == "CASH"
    assert samsung["payment_date"] == "2026-04-15"
    assert samsung["gross_amount"] == 36100.0
    assert samsung["tax_amount"] == 5550.0
    assert samsung["net_amount"] == 30550.0
    assert samsung["source"] == "AUTO_KIS"

    kodex = next(r for r in rows if r["symbol"] == "069500")
    assert kodex["dividend_type"] == "ETF_DIST"


def test_ui_common_api_post(monkeypatch):
    from ui.common import api_post
    import requests

    called_kwargs = {}

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    def _mock_post(url, **kwargs):
        called_kwargs.update(kwargs)
        return DummyResponse()

    monkeypatch.setattr(requests, "post", _mock_post)

    # 1. params 전달 테스트 (이번 에러의 직접적 원인)
    res = api_post("/test", params={"start_date": "20250101", "end_date": "20261006"})
    assert res == {"status": "ok"}
    assert called_kwargs.get("params") == {"start_date": "20250101", "end_date": "20261006"}

    # 2. json 키워드 인수 전달 테스트
    called_kwargs.clear()
    res = api_post("/test", json={"a": 1})
    assert res == {"status": "ok"}
    assert called_kwargs.get("json") == {"a": 1}

    # 3. data 및 headers 전달 테스트
    called_kwargs.clear()
    res = api_post("/test", data=b"raw", headers={"Content-Type": "text/plain"})
    assert res == {"status": "ok"}
    assert called_kwargs.get("data") == b"raw"
    assert called_kwargs.get("headers") == {"Content-Type": "text/plain"}

