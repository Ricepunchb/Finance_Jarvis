#!/usr/bin/env python3
"""일회성: 국내 전용 피봇 전에 사둔 해외 보유분을 미국장 시간대에 자동으로 정리한다.

DOMESTIC_ONLY 모드의 엔진은 해외 종목을 매매도 정리도 하지 않으므로 이 스크립트가 따로 돈다.
엔진의 해외 처리 메서드(손절/트레일링 -> 시그널 -> 주문)를 그대로 재사용하고, 보유 전량을 "정리 대기"로 다룬다:
  - 평시: 결합 시그널이 SELL일 때만 전량 매도(1회 주문 한도로 나뉘면 이후엔 신호와 무관하게 끝까지).
  - 마지막 24시간 안에 열리는 세션부터: 신호와 무관하게 전량 매도.
  - 신규 매수는 절대 하지 않는다. 손절/트레일링익절은 평소대로 먼저 평가된다.
실제 해외 잔고를 매 사이클 조회해 보유가 없으면 스스로 종료한다. 기간(--days)이 끝나도 종료한다.

  uv run python scripts/liquidate_overseas_once.py --dry-run     # 해외 보유 조회만 (주문 없음)
  uv run python scripts/liquidate_overseas_once.py               # 실행 (보통 nohup으로 백그라운드)
재시작하면 data/overseas_liquidation_state.json의 시작시각을 이어받는다 (기한이 늘어나지 않는다).
"""
import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402

settings.DOMESTIC_ONLY = False  # 이 프로세스에서만: 해외 대조/조회 경로를 켠다 (.env와 실행 중인 엔진은 그대로)

from core import db, intraday, kis_domestic, kis_overseas, reconciliation  # noqa: E402
from core.engine import CYCLE_INTERVAL_SEC, TradingEngine, _is_us_market_open_now  # noqa: E402
from core.lock import EngineAlreadyRunningError, InstanceLock  # noqa: E402
from core.risk import RiskManager  # noqa: E402

logger = logging.getLogger("liquidate_overseas")

STATE_PATH = ROOT / "data" / "overseas_liquidation_state.json"
LOG_PATH = ROOT / "data" / "overseas_liquidation.log"
LOCK_PATH = ROOT / "data" / "overseas_liquidation.lock"
FORCE_LAST_HOURS = 24      # 기한 마지막 이 시간 안에 시작되는 세션부터는 신호와 무관하게 전량 매도
MAX_REJECTIONS_PER_CYCLE = 2


class _ScriptRisk(RiskManager):
    """주문 거부를 전역 킬스위치로 올리지 않는다 — 이 스크립트의 거부(휴장 등)로 국내 엔진까지
    멈추면 안 된다. 대신 사이클 안에서 거부가 몰리면 그 사이클의 남은 종목 주문을 접는다."""

    def __init__(self, conn):
        super().__init__(conn)
        self.rejections_this_cycle = 0

    async def record_order_rejection(self) -> None:
        self.rejections_this_cycle += 1
        logger.warning(f"주문 거부 {self.rejections_this_cycle}회째 (이번 사이클)")


def load_state(days: int, restart: bool) -> dict:
    if STATE_PATH.exists() and not restart:
        state = json.loads(STATE_PATH.read_text())
        state["days"] = days
        return state
    return {"started_at": time.time(), "days": days, "sell_started": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


async def fetch_overseas(eng: TradingEngine) -> dict:
    """실제 해외 보유 (symbol -> 엔진의 _process_overseas_symbol이 읽는 형태의 행). positions도 동기화한다.

    수량/평단/거래소는 외화 기준 잔고조회(inquire-balance)에서 읽는다. 체결기준현재잔고(present-balance)는
    모의투자에서 종목 행은 주면서 수량(cblc_qty13)을 전부 0으로 줘서, 그것만 믿으면 "보유 없음"으로 오판한다.
    present-balance는 기준환율과 합계 용도로만 쓴다. 조회가 하나라도 실패하면 예외 - "없음"으로 취급하지 않는다."""
    present = await kis_overseas.get_present_balance_krw(eng.client)
    exrt = {h["pdno"]: h.get("bass_exrt") for h in present["holdings"] if h.get("pdno")}
    held: dict = {}
    for exch in ("NASD", "NYSE", "AMEX"):  # 모의투자는 거래소와 무관하게 미국 보유 전체를 돌려주기도 해 종목 단위로 중복 제거
        bal = await kis_overseas.get_balance(eng.client, exch, "USD")
        for h in bal["holdings"]:
            symbol, qty = h.get("ovrs_pdno"), float(h.get("ovrs_cblc_qty") or 0)
            if not symbol or qty <= 0 or symbol in held:
                continue
            held[symbol] = {
                "pdno": symbol, "prdt_name": h.get("ovrs_item_name"), "cblc_qty13": qty,
                "ord_psbl_qty1": h.get("ord_psbl_qty"), "avg_unpr3": h.get("pchs_avg_pric"),
                "crcy_cd": h.get("tr_crcy_cd") or "USD", "ovrs_excg_cd": h.get("ovrs_excg_cd") or exch,
                "ovrs_now_pric1": h.get("now_pric2"), "bass_exrt": exrt.get(symbol),
            }
    await db.sync_positions_from_balance(
        eng.conn,
        {s: (h["cblc_qty13"], float(h.get("avg_unpr3") or 0), h["crcy_cd"]) for s, h in held.items()},
        overseas=True,
    )
    return {"held": held, "totals": present["totals"] or {}}


async def known_exchanges(conn) -> dict:
    cur = await conn.execute("SELECT symbol, exchange FROM portfolio_symbols WHERE market = 'overseas' AND exchange IS NOT NULL")
    return {r["symbol"]: r["exchange"] for r in await cur.fetchall()}


def exchange_of(symbol: str, row: dict, known: dict):
    return row.get("ovrs_excg_cd") or known.get(symbol)


async def run_cycle(eng: TradingEngine, state: dict, force_all: bool) -> bool:
    """한 사이클. 해외 보유가 없어진 것이 확인되면 True(= 종료)."""
    try:
        await reconciliation.reconcile_unresolved_intents(eng.client, eng.conn, min_age_sec=60)
    except Exception:
        logger.exception("체결 대조 실패 - 계속")

    snap = await fetch_overseas(eng)
    held = snap["held"]
    if not held:
        logger.info("해외 보유 없음 - 정리 완료")
        return True

    balance = await kis_domestic.get_balance(eng.client)
    total_equity = float(balance["summary"].get("nass_amt") or 0) + float(snap["totals"].get("tot_asst_amt") or 0)
    known = await known_exchanges(eng.conn)
    cycle_id = await db.new_cycle(eng.conn)
    eng._intraday_backfill_budget = intraday.BackfillBudget(settings.INTRADAY_BACKFILL_SYMBOLS_PER_CYCLE)
    eng.risk.rejections_this_cycle = 0
    logger.info(f"사이클 {cycle_id}: 보유 {len(held)}종목 {sorted(held)} (강제청산={'예' if force_all else '아니오'})")

    for symbol, row in held.items():
        exchange = exchange_of(symbol, row, known)
        if not exchange:
            logger.warning(f"'{symbol}' 거래소를 알 수 없어 건너뜀 (포트폴리오 DB/잔고 응답 모두에 없음) - 수동 확인 필요")
            continue
        liquidating = force_all or symbol in state["sell_started"]
        try:
            # ws_ok=True: 체결통보 웹소켓은 국내 전용이라 해외 체결을 알려주지 않는다. 이중주문은
            # 종목별 쿨다운 + 주문직전 실시간 매도가능수량(미체결 차감)으로 막고, 체결은 매 사이클 잔고로 확인한다.
            await eng._process_overseas_symbol(
                cycle_id, symbol, exchange, 0.0, row, total_equity, True, winddown_liquidating=liquidating,
            )
        except Exception:
            logger.exception(f"'{symbol}' 처리 중 예외 - 이 종목만 건너뜀")

        cur = await eng.conn.execute(
            "SELECT 1 FROM order_intents WHERE symbol = ? AND cycle_id = ? AND side = 'sell' "
            "AND reason LIKE 'winddown%' AND status != 'REJECTED'", (symbol, cycle_id),
        )
        if await cur.fetchone() and symbol not in state["sell_started"]:
            state["sell_started"][symbol] = time.time()
            save_state(state)
            logger.info(f"'{symbol}' 정리 매도 시작 - 이후 신호와 무관하게 끝까지 매도")
        if eng.risk.rejections_this_cycle >= MAX_REJECTIONS_PER_CYCLE:
            logger.warning("이번 사이클 주문 거부가 몰려 남은 종목은 다음 사이클로 미룸 (휴장/장애 의심)")
            break
    return False


async def sleep_or_stop(stop: asyncio.Event, sec: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=sec)
    except asyncio.TimeoutError:
        pass


async def main(args) -> int:
    settings.assert_trading_allowed()
    eng = TradingEngine()
    await db.init_db()
    eng.conn = await db.get_connection()
    eng.risk = _ScriptRisk(eng.conn)

    if args.dry_run:
        snap = await fetch_overseas(eng)
        known = await known_exchanges(eng.conn)
        print(f"모드: {'모의투자' if settings.IS_MOCK else '실전투자'} / 해외 보유 {len(snap['held'])}종목")
        for s, h in snap["held"].items():
            print(f"  {s} {h.get('prdt_name')}: {h.get('cblc_qty13'):g}주 (주문가능 {h.get('ord_psbl_qty1')}), "
                  f"평단 {h.get('avg_unpr3')} {h.get('crcy_cd')}, 현재가 {h.get('ovrs_now_pric1')}, 거래소 {exchange_of(s, h, known)}")
        await eng.conn.close()
        return 0

    lock = InstanceLock(str(LOCK_PATH))
    try:
        lock.acquire()
    except EngineAlreadyRunningError:
        print("이미 실행 중입니다.")
        return 1

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    state = load_state(args.days, args.restart)
    save_state(state)
    end = state["started_at"] + args.days * 86400
    force_from = end - FORCE_LAST_HOURS * 3600
    logger.info(f"시작 ({'모의' if settings.IS_MOCK else '실전'}) - 종료 예정 {time.strftime('%m-%d %H:%M', time.localtime(end))}, "
                f"강제청산 시작 {time.strftime('%m-%d %H:%M', time.localtime(force_from))}")

    if settings.GEMINI_API_KEY:
        try:
            from core.llm.factory import get_llm_provider
            eng.llm_provider = get_llm_provider()
        except Exception:
            logger.exception("LLM 초기화 실패 - 기술적 지표만 사용")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        try:  # 시작 즉시 한 번 확인 - 장이 닫혀 있어도 로그에 무엇을 정리할지 남기고, 이미 없으면 바로 종료
            snap = await fetch_overseas(eng)
            logger.info(f"시작 시점 해외 보유: { {s: h.get('cblc_qty13') for s, h in snap['held'].items()} }")
            if not snap["held"]:
                logger.info("해외 보유 없음 - 종료")
                return 0
        except Exception:
            logger.exception("시작 시점 해외 잔고 조회 실패 - 첫 사이클에 재시도")

        next_cycle_at = 0.0
        while not stop.is_set() and time.time() < end:
            if _is_us_market_open_now() and time.time() >= next_cycle_at:
                if await eng.risk.is_kill_switch_active():
                    logger.warning("킬스위치 활성 - 5분 후 재확인")
                    next_cycle_at = time.time() + 300
                else:
                    wait = CYCLE_INTERVAL_SEC  # 사이클 "종료" 후 30분 대기 (엔진과 동일) - 직전 주문이 쿨다운(30분) 안에 다시 걸리지 않게
                    try:
                        if await run_cycle(eng, state, force_all=time.time() >= force_from):
                            return 0
                    except Exception:
                        logger.exception("사이클 실패(토큰 발급 제한/API 장애 등) - 2분 후 재시도")
                        wait = 120  # 주문 전에 실패한 경우라 쿨다운과 무관 - 30분을 통째로 버리지 않는다
                    next_cycle_at = time.time() + wait
            await sleep_or_stop(stop, 30)

        if stop.is_set():
            logger.info("중지 신호 수신 - 종료")
        else:
            try:
                left = (await fetch_overseas(eng))["held"]
                logger.info(f"기한 종료 - 남은 해외 보유: { {s: h.get('cblc_qty13') for s, h in left.items()} or '없음' }")
            except Exception:
                logger.exception("기한 종료 시점 잔고 조회 실패")
        return 0
    finally:
        await eng.conn.close()
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=5, help="실행 기간(일, 기본 5)")
    parser.add_argument("--dry-run", action="store_true", help="해외 보유만 조회하고 종료 (주문 없음)")
    parser.add_argument("--restart", action="store_true", help="이전 상태 파일을 무시하고 지금부터 다시 시작")
    sys.exit(asyncio.run(main(parser.parse_args())))
