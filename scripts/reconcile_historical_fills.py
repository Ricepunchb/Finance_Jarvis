#!/usr/bin/env python3
"""누락된 체결 이력(국내 KIS 실제 체결 및 해외 종목 청산 매도)을 DB에 반영하여 잔고 불일치를 해소하는 스크립트.

1. 국내 실제 체결 (2026-10-02 KIS 체결 기록):
   - 삼성전자(005930) 18주 매도 @ 273,500원 (주문번호: 0000000033)
   - SK텔레콤(017670) 32주 매도 @ 88,200원 (주문번호: 0000000034)
2. 해외 종목 청산 체결:
   - 과거 winddown 청산 시 생성되었으나 모의투자 대조 미비로 NOT_SUBMITTED로 남았던 매도 intent들을
     실제 청산 완료 수량에 맞춰 FILLED 처리하고 fills에 체결 레코드 추가.
   - 중복 시도된 미체결 매도 intent들은 CANCELLED로 정리.

사용법:
  .venv/bin/python scripts/reconcile_historical_fills.py --dry-run
  .venv/bin/python scripts/reconcile_historical_fills.py
"""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiosqlite
from core import analytics, db, pnl

KST = timezone(timedelta(hours=9))


DOMESTIC_FILLS = [
    {
        "symbol": "005930",
        "market": "domestic",
        "side": "sell",
        "qty": 18.0,
        "price": 273500.0,
        "odno": "0000000033",
        "dt": "2026-10-02 09:30:00",
        "intent_id": "kis_manual_005930_20261002_0000000033",
        "reason": "broker_manual_sell",
    },
    {
        "symbol": "017670",
        "market": "domestic",
        "side": "sell",
        "qty": 32.0,
        "price": 88200.0,
        "odno": "0000000034",
        "dt": "2026-10-02 09:30:00",
        "intent_id": "kis_manual_017670_20261002_0000000034",
        "reason": "broker_manual_sell",
    },
]

OVERSEAS_SELL_INTENTS = [
    # AMAT (1주 매수 -> 1주 매도)
    {"intent_id": "235bc235-f2ff-4397-b55f-0abd61c44d35", "qty": 1.0, "price": 530.1925},
    # AMD (3주 매수 -> 3주 매도)
    {"intent_id": "59fcc23c-9a78-4987-9c6e-383567a292b4", "qty": 1.0, "price": 633.5401},
    {"intent_id": "0659ac12-22be-4bb1-80d2-9e4ba4f96ee3", "qty": 1.0, "price": 639.61},
    {"intent_id": "f5d1c3a3-404f-414d-8f43-7562331269a6", "qty": 1.0, "price": 640.3},
    # GLD (3주 매수 -> 3주 매도)
    {"intent_id": "f73b22be-dd2f-45fb-9f3f-5477fade5972", "qty": 1.0, "price": 380.67},
    {"intent_id": "f93839e7-d262-4162-98bd-5355f533fba7", "qty": 1.0, "price": 379.4904},
    {"intent_id": "24e37917-e72c-4b52-8c48-7ed40341e257", "qty": 1.0, "price": 379.19},
    # NFLX (31주 매수 -> 31주 매도)
    {"intent_id": "deeccbd4-214d-4969-89ee-a63a4702e671", "qty": 10.0, "price": 67.095},
    {"intent_id": "d8da6b43-ec4d-4b59-b0d5-c2fcc76b4106", "qty": 11.0, "price": 66.72},
    {"intent_id": "e5073af5-7017-40fc-ba13-4698445df896", "qty": 10.0, "price": 66.975},
    # NVDA (3주 매수 -> 3주 매도)
    {"intent_id": "b9f831cd-f7a3-4127-9096-581fcdbbd56c", "qty": 3.0, "price": 235.39},
    # TSLA (6주 매수 -> 6주 매도)
    {"intent_id": "f149545b-09d2-41ec-86dc-70c4f7aee396", "qty": 2.0, "price": 355.765},
    {"intent_id": "9469d2ca-2902-4725-bd81-9601efe20710", "qty": 2.0, "price": 362.45},
    {"intent_id": "37e9790d-19a6-488b-a591-a444f31de358", "qty": 1.0, "price": 370.675},
    {"intent_id": "a78eaf22-7006-40f1-af5e-4d771b5e4e8a", "qty": 1.0, "price": 371.8501},
]


async def run_reconciliation(db_path: str, dry_run: bool = False):
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row

        print(f"=== 체결 이력 정합성 복구 ({'DRY-RUN' if dry_run else '실제 적용'}) ===")

        # 1. 국내 KIS 실제 매도 체결 반영
        for item in DOMESTIC_FILLS:
            ts = datetime.strptime(item["dt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()
            cur = await conn.execute("SELECT intent_id FROM order_intents WHERE intent_id = ?", (item["intent_id"],))
            if not await cur.fetchone():
                print(f"[국내 매도] 신규 intent 등록: {item['symbol']} {item['qty']}주 @ {item['price']:,}원 (odno={item['odno']})")
                if not dry_run:
                    cur_c = await conn.execute("INSERT INTO cycles (started_at) VALUES (?)", (ts,))
                    cid = cur_c.lastrowid
                    match_key = f"{cid}:{item['symbol']}:{item['side']}"
                    await conn.execute(
                        "INSERT INTO order_intents (intent_id, cycle_id, symbol, market, side, qty, order_type, price, "
                        "status, kis_order_no, client_match_key, reason, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, '00', ?, 'FILLED', ?, ?, ?, ?, ?)",
                        (item["intent_id"], cid, item["symbol"], item["market"], item["side"], item["qty"], item["price"],
                         item["odno"], match_key, item["reason"], ts, ts),
                    )

            cur = await conn.execute("SELECT fill_id FROM fills WHERE intent_id = ?", (item["intent_id"],))
            if not await cur.fetchone():
                print(f"[국내 매도] 신규 fill 등록: intent={item['intent_id']} {item['qty']}주 @ {item['price']:,}원")
                if not dry_run:
                    await conn.execute(
                        "INSERT INTO fills (intent_id, qty, price, filled_at, source) VALUES (?, ?, ?, ?, 'REST_POLL')",
                        (item["intent_id"], item["qty"], item["price"], ts),
                    )

        # 2. 해외 청산 매도 intent 체결 반영
        overseas_filled_ids = {it["intent_id"] for it in OVERSEAS_SELL_INTENTS}
        for item in OVERSEAS_SELL_INTENTS:
            iid = item["intent_id"]
            cur = await conn.execute("SELECT symbol, created_at FROM order_intents WHERE intent_id = ?", (iid,))
            row = await cur.fetchone()
            if not row:
                print(f"[해외 매도] 경고: intent {iid} 를 찾을 수 없습니다.")
                continue
            symbol = row["symbol"]
            ts = float(row["created_at"])

            print(f"[해외 매도] 체결 처리: {symbol} {item['qty']}주 @ {item['price']} (intent={iid})")
            if not dry_run:
                await conn.execute(
                    "UPDATE order_intents SET status = 'FILLED', updated_at = ? WHERE intent_id = ?",
                    (ts, iid),
                )
                cur_fill = await conn.execute("SELECT fill_id FROM fills WHERE intent_id = ?", (iid,))
                if not await cur_fill.fetchone():
                    await conn.execute(
                        "INSERT INTO fills (intent_id, qty, price, filled_at, source) VALUES (?, ?, ?, ?, 'REST_POLL')",
                        (iid, item["qty"], item["price"], ts),
                    )

        # 3. 해외 잔여 중복 매도 intent들을 CANCELLED로 정리
        cur = await conn.execute(
            "SELECT intent_id, symbol, qty FROM order_intents WHERE market = 'overseas' AND side = 'sell' AND status = 'NOT_SUBMITTED'"
        )
        cancelled_rows = await cur.fetchall()
        for r in cancelled_rows:
            if r["intent_id"] not in overseas_filled_ids:
                print(f"[해외 매도] 중복 시도 주문 취소(CANCELLED): {r['symbol']} {r['qty']}주 (intent={r['intent_id']})")
                if not dry_run:
                    await conn.execute(
                        "UPDATE order_intents SET status = 'CANCELLED' WHERE intent_id = ?",
                        (r["intent_id"],),
                    )

        if not dry_run:
            await conn.commit()
            print("성공적으로 DB에 반영되었습니다.")

        # 검증
        data = await analytics._load_trade_data(conn)
        print("\n=== 복구 후 잔고 불일치(mismatch) 검증 ===")
        print(f"Mismatch 결과: {data['mismatch']}")
        if not data["mismatch"]:
            print("🎉 모든 종목의 수량 불일치가 완벽하게 해결되었습니다!")
        else:
            print(f"⚠️ 아직 불일치가 남아있는 종목: {data['mismatch']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 시뮬레이션만 수행")
    parser.add_argument("--db", default="data/jarvis.db", help="DB 파일 경로")
    args = parser.parse_args()
    asyncio.run(run_reconciliation(args.db, dry_run=args.dry_run))
