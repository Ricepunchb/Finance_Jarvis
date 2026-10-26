# core/portfolio_scheduler.py
"""AI 포트폴리오 에이전트의 백그라운드 트리거 루프.

TradingEngine과 완전히 독립적으로 동작한다 — 트레이딩 엔진이 꺼져 있어도(주말 리서치 등)
리밸런싱 제안 자체는 가능해야 하고, 서로 다른 장애 도메인(주/월 단위 LLM 호출 vs 30분
주문 실행 핫루프)을 한 루프에 섞지 않기 위함이다. `api/main.py`가 만든 공유
AsyncKISClient(engine.client)를 그대로 받아쓴다 — KIS는 토큰을 새로 발급할 때마다
카카오톡 알림을 보내므로, 별도 클라이언트를 새로 만들어 불필요한 토큰을 추가로
발급받지 않기 위함이다.

기본적으로 아무 것도 하지 않는다 - engine_state의 ai_rebalance_scheduler_enabled가
"1"일 때만 실제로 트리거를 검사한다 (기본값 꺼짐, 사용자가 명시적으로 켜야 함).
"""
import ast
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from core import db, discovery, discovery_sources, kis_domestic, portfolio_agent, proxy_mapping, rebalancer
from core.config import settings
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

CHECK_INTERVAL_SEC = 60  # 정밀 스케줄링 폴링 주기 (1분) - 정각(09, 12, 15, 21시) 도달 시 즉시 트리거
REBALANCE_CHECK_INTERVAL_SEC = 3600  # 리밸런스 드리프트/뉴스 감성 검사 주기 (1시간)
STATE_DISCOVERY_TRIGGERED = "ai_rebalance_discovery_triggered_json"


class PortfolioScheduler:
    def __init__(self, client: AsyncKISClient):
        self.client = client
        self._stopped = True
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while not self._stopped:
            try:
                await self._tick()
            except Exception:
                logger.exception("포트폴리오 스케줄러 tick 처리 중 예외 - 다음 tick으로 계속")
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
            except asyncio.CancelledError:
                return

    async def _tick(self) -> None:
        conn = await db.get_connection()
        try:
            refreshed = await self._maybe_refresh_discovery(conn)

            # 리밸런싱 트리거(드리프트/뉴스/발굴) 검사는 1시간(3600초) 간격으로만 실행하여 불필요한 KIS 잔고 조회 방지.
            # 단, 방금 발굴 갱신이 실행된 직후(refreshed=True)에는 신규 고득점 발굴 종목을 즉시 평가하도록 스로틀을 건너뛴다.
            last_reb_check = await db.get_state(conn, "last_rebalance_trigger_check_at")
            if (
                not refreshed
                and last_reb_check is not None
                and (time.time() - float(last_reb_check)) < REBALANCE_CHECK_INTERVAL_SEC
            ):
                return

            if await db.get_state(conn, "ai_rebalance_scheduler_enabled") != "1":
                return
            if await self._cooldown_active(conn):
                return

            await db.set_state(conn, "last_rebalance_trigger_check_at", str(time.time()))

            if await self._periodic_due(conn):
                logger.info("AI 포트폴리오 에이전트: 정기 재검토 트리거")
                res = await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="SCHEDULED",
                    trigger_detail=f"{settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS}일 정기 재검토",
                )
                await self._maybe_auto_apply(conn, res)
                await self._mark_decision(conn)
                return

            discovery_detail = await self._discovery_trigger_detail(conn)
            if discovery_detail:
                logger.info(f"AI 포트폴리오 에이전트: 발굴 트리거 - {discovery_detail}")
                res = await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="DISCOVERY", trigger_detail=discovery_detail,
                )
                await self._maybe_auto_apply(conn, res)
                await self._mark_decision(conn)
                return

            drift_detail = await self._drift_trigger_detail(conn)
            if drift_detail:
                logger.info(f"AI 포트폴리오 에이전트: 드리프트 트리거 - {drift_detail}")
                res = await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="DRIFT", trigger_detail=drift_detail,
                )
                await self._maybe_auto_apply(conn, res)
                await self._mark_decision(conn)
                return

            news_detail = await self._news_event_trigger_detail(conn)
            if news_detail:
                logger.info(f"AI 포트폴리오 에이전트: 뉴스이벤트 트리거 - {news_detail}")
                res = await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="NEWS_EVENT", trigger_detail=news_detail,
                )
                await self._maybe_auto_apply(conn, res)
                await self._mark_decision(conn)
        finally:
            await conn.close()

    async def _maybe_auto_apply(self, conn, res: Optional[dict]) -> None:
        if not res or res.get("status") != "PROPOSED":
            return
        is_auto = settings.AI_REBALANCE_AUTO_APPLY
        state_val = await db.get_state(conn, "ai_rebalance_auto_apply")
        if state_val is not None:
            is_auto = (state_val == "1")
        if is_auto:
            event_id = res.get("event_id")
            if event_id:
                logger.info(f"AI 포트폴리오 에이전트: auto_apply 활성 — 이벤트 #{event_id} 자동 승인 및 포트폴리오 반영")
                await db.apply_rebalance_event(conn, event_id, approve=True, decided_by="auto_apply")

    async def _maybe_refresh_discovery(self, conn) -> bool:
        """AI 인사이트 및 종목 발굴을 정기 스케줄(아침 9시, 오후 12시, 오후 3시, 오후 9시 KST)에 맞춰 자동 실행한다."""
        if not settings.DISCOVERY_DYNAMIC_ENABLED and not settings.INSIGHT_ENABLED:
            return False

        now_kst = datetime.now(tz=KST)
        schedule_hours = getattr(settings, "INSIGHT_SCHEDULE_HOURS_KST", [9, 12, 15, 21])
        if now_kst.hour not in schedule_hours:
            return False

        # 당일 해당 시각 슬롯(예: '20261023_09')에 이미 실행되었는지 검사 (동일 슬롯 중복 실행 방지)
        slot_key = f"{now_kst.strftime('%Y%m%d')}_{now_kst.hour:02d}"
        last_slot = await db.get_state(conn, "last_insight_schedule_slot")
        if last_slot == slot_key:
            return False

        logger.info(
            f"AI 인사이트 & 발굴 정기 스케줄러 트리거 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')} KST, 슬롯={slot_key})"
        )
        await db.set_state(conn, "last_insight_schedule_slot", slot_key)
        try:
            await discovery_sources.refresh_dynamic_universe(conn, self.client, trigger_type="SCHEDULED")
            return True
        except Exception:
            logger.exception("AI 인사이트 & 동적 종목 발굴 갱신 실패 - 다음 tick에 재시도")
            return False

    async def _cooldown_active(self, conn) -> bool:
        raw = await db.get_state(conn, "ai_rebalance_last_decision_at")
        if raw is None:
            return False
        return (time.time() - float(raw)) < settings.AI_REBALANCE_MIN_INTERVAL_SEC

    async def _mark_decision(self, conn) -> None:
        await db.set_state(conn, "ai_rebalance_last_decision_at", str(time.time()))

    async def _periodic_due(self, conn) -> bool:
        raw = await db.get_state(conn, "ai_rebalance_last_scheduled_run_at")
        if raw is None:
            due = True
        else:
            elapsed_days = (time.time() - float(raw)) / 86400
            due = elapsed_days >= settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS
        if due:
            await db.set_state(conn, "ai_rebalance_last_scheduled_run_at", str(time.time()))
        return due

    async def _discovery_trigger_detail(self, conn) -> Optional[str]:
        """신선한 발굴 캐시에서 아직 보유하지 않았고 최근 트리거된 적 없는 고득점(>= AI_REBALANCE_DISCOVERY_TRIGGER_SCORE)
        우량 후보가 출현했을 때 정기 주기를 기다리지 않고 즉시 리밸런싱을 트리거한다 (KIS 호출 없음)."""
        active_weights = await db.get_active_target_weights(conn)
        if not active_weights:
            return None

        cache = await discovery.load_scored_cache(conn)
        if not cache:
            return None
        now = time.time()
        scored_at = float(cache.get("scored_at") or 0.0)
        if (now - scored_at) >= settings.DISCOVERY_SHORTLIST_TTL_HOURS * 3600:
            return None

        enabled = {c["symbol"] for c in await db.list_candidate_universe(conn)}
        recently_removed = set(
            await db.list_recently_removed_symbols(conn, settings.AI_REBALANCE_REMOVED_COOLDOWN_DAYS)
        )
        held_or_excluded = set(active_weights.keys()) | recently_removed
        proxy_enabled = await portfolio_agent.get_effective_proxy_trading(conn)

        expiry_sec = settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS * 86400
        raw_trig = await db.get_state(conn, STATE_DISCOVERY_TRIGGERED)
        try:
            triggered_map = json.loads(raw_trig) if raw_trig else {}
        except Exception:
            triggered_map = {}
        triggered_map = {
            s: float(ts) for s, ts in triggered_map.items() if (now - float(ts)) < expiry_sec
        }

        min_score = settings.AI_REBALANCE_DISCOVERY_TRIGGER_SCORE
        qualifying = []
        for e in cache.get("entries", []):
            sym = e.get("symbol")
            if not sym or sym not in enabled or sym in held_or_excluded or sym in triggered_map:
                continue
            if proxy_enabled:
                proxy_info = proxy_mapping.get_proxy_etf(sym)
                if proxy_info and (
                    proxy_info["proxy_symbol"] in held_or_excluded
                    or proxy_info["proxy_symbol"] in triggered_map
                ):
                    continue
            score = float(e.get("score") or 0.0)
            if score >= min_score:
                qualifying.append(e)

        if not qualifying:
            return None

        qualifying.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        for e in qualifying:
            sym = e["symbol"]
            triggered_map[sym] = now
            if proxy_enabled:
                proxy_info = proxy_mapping.get_proxy_etf(sym)
                if proxy_info:
                    triggered_map[proxy_info["proxy_symbol"]] = now

        await db.set_state(conn, STATE_DISCOVERY_TRIGGERED, json.dumps(triggered_map, ensure_ascii=False))

        top = qualifying[0]
        angle_str = top.get("angle_label") or top.get("angle") or "복합"
        extra = f" 외 {len(qualifying) - 1}건" if len(qualifying) > 1 else ""
        return f"고득점 발굴 포착: {top['symbol']}({top.get('name', '')}) 점수 {float(top['score']):.2f} [{angle_str}]{extra}"

    async def _drift_trigger_detail(self, conn) -> Optional[str]:
        """국내 종목만 검사한다 - 해외는 환율까지 조회해야 해 이 저빈도 체크의 범위를
        벗어난다 (해외 종목의 드리프트는 정기(SCHEDULED) 트리거가 대신 커버한다)."""
        active_weights = await db.get_active_target_weights(conn)
        if not active_weights:
            return None
        symbol_info = await db.get_portfolio_symbol_info(conn)
        positions = await db.get_positions(conn)
        domestic_symbols = [
            s for s in active_weights
            if (symbol_info.get(s) or {}).get("market", "domestic") == "domestic"
        ]
        if not domestic_symbols:
            return None

        try:
            balance = await kis_domestic.get_balance(self.client)
        except Exception:
            logger.exception("드리프트 트리거 확인 중 잔고조회 실패 - 이번 tick은 건너뜀")
            return None
        total_equity = float(balance["summary"].get("nass_amt") or 0)
        if total_equity <= 0:
            return None

        for symbol in domestic_symbols:
            try:
                price_info = await kis_domestic.get_price(self.client, symbol)
                price = float(price_info.get("stck_prpr") or 0)
            except Exception:
                continue
            if price <= 0:
                continue
            qty = float((positions.get(symbol) or {}).get("qty") or 0)
            current_weight = (qty * price) / total_equity
            target = active_weights[symbol]
            drift = rebalancer.compute_drift(current_weight, target, settings.REBALANCE_BAND_PCT)
            if not drift.in_band and abs(drift.drift) > (
                settings.REBALANCE_BAND_PCT + settings.AI_REBALANCE_DRIFT_TRIGGER_BUFFER_PCT
            ):
                return f"{symbol} 드리프트 {drift.drift:+.1%} (목표 {target:.1%}, 현재 {current_weight:.1%})"
        return None

    async def _news_event_trigger_detail(self, conn) -> Optional[str]:
        """TradingEngine이 30분 사이클마다 이미 계산해 decision_log에 남긴 뉴스감성을
        재사용한다 - 별도 뉴스 폴링 인프라를 새로 만들지 않는다."""
        active_weights = await db.get_active_target_weights(conn)
        if not active_weights:
            return None
        raw = await db.get_state(conn, "ai_rebalance_last_decision_at")
        since_ts = float(raw) if raw is not None else (time.time() - 86400)

        cur = await conn.execute(
            "SELECT symbol, sentiment_signal FROM decision_log WHERE ts > ? "
            "AND sentiment_signal IS NOT NULL AND sentiment_signal != 'None' "
            "ORDER BY id DESC LIMIT 200",
            (since_ts,),
        )
        rows = await cur.fetchall()
        for row in rows:
            if row["symbol"] not in active_weights:
                continue
            try:
                parsed = ast.literal_eval(row["sentiment_signal"])
                strength = float(parsed.get("strength") or 0)
                direction = parsed.get("direction")
            except Exception:
                continue
            if strength >= settings.AI_REBALANCE_NEWS_TRIGGER_STRENGTH:
                return f"{row['symbol']} 뉴스감성 강도 {strength:.2f} ({direction})"
        return None
