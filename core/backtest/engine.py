# core/backtest/engine.py
"""이벤트 기반 포트폴리오 백테스팅 시뮬레이터.

Finance Jarvis의 실제 운영 엔진 규칙을 1:1로 반영합니다:
1. 손절(-7%) 및 ATR 트레일링 익절 (최우선 평가, exit_guard.evaluate_exit)
2. 기술적 지표 연속값 시그널 (RSI, MACD, BBands, indicators.compute_technical_signal)
3. 밴드 상한 강제 축소 및 분할 온보딩 (signal_engine)
4. 거래 비용 모델링 (수수료, 증권거래세, 슬리피지)
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from core import exit_guard, indicators, signal_engine
from core.config import settings


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    peak_price: float
    entry_date: str
    entry_avg_price: float


@dataclass
class TradeRecord:
    symbol: str
    date: str
    side: str  # "buy" | "sell"
    price: float
    qty: float
    notional: float
    fee: float
    tax: float
    reason: str
    realized_pnl: float = 0.0
    return_pct: float = 0.0


@dataclass
class DailySnapshot:
    date: str
    cash: float
    positions_value: float
    total_equity: float
    daily_return: float
    positions: Dict[str, Dict[str, float]]  # {symbol: {"qty": qty, "price": close, "value": value}}


@dataclass
class BacktestResult:
    initial_cash: float
    final_equity: float
    total_return_pct: float
    daily_snapshots: List[DailySnapshot]
    trades: List[TradeRecord]
    symbols: List[str]
    start_date: str
    end_date: str


class BacktestEngine:
    def __init__(
        self,
        symbols: List[str],
        data_by_symbol: Dict[str, pd.DataFrame],
        initial_cash: float = 10_000_000.0,
        target_weights: Optional[Dict[str, float]] = None,
        fee_rate: float = settings.BACKTEST_DEFAULT_FEE_PCT,
        tax_rate: float = settings.BACKTEST_DEFAULT_TAX_PCT,
        slippage_pct: float = settings.BACKTEST_DEFAULT_SLIPPAGE_PCT,
        stop_loss_pct: float = settings.STOP_LOSS_PCT,
        onboarding_days: int = settings.ONBOARDING_DAYS,
        max_position_pct: float = settings.MAX_POSITION_PCT,
        max_order_notional: float = float(settings.MAX_ORDER_NOTIONAL_KRW),
    ):
        self.symbols = symbols
        self.data = data_by_symbol
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fee_rate = fee_rate
        self.tax_rate = tax_rate
        self.slippage_pct = slippage_pct
        self.stop_loss_pct = stop_loss_pct
        self.onboarding_days = onboarding_days
        self.max_position_pct = max_position_pct
        self.max_order_notional = max_order_notional

        # 균등 비중 기본값
        if target_weights is None or not target_weights:
            eq_weight = round(1.0 / max(1, len(symbols)), 4)
            self.target_weights = {s: eq_weight for s in symbols}
        else:
            self.target_weights = target_weights

        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeRecord] = []
        self.snapshots: List[DailySnapshot] = []

    def run(self, start_date: str, end_date: str) -> BacktestResult:
        # 거래일 집합 생성 (지표 콜드스타트 이후 구간 대상)
        all_dates = set()
        for df in self.data.values():
            if not df.empty:
                df_sub = df[(df["date"] >= pd.to_datetime(start_date)) & (df["date"] <= pd.to_datetime(end_date))]
                all_dates.update(df_sub["date"].dt.strftime("%Y-%m-%d").tolist())

        trading_dates = sorted(list(all_dates))
        if not trading_dates:
            return BacktestResult(
                initial_cash=self.initial_cash,
                final_equity=self.initial_cash,
                total_return_pct=0.0,
                daily_snapshots=[],
                trades=[],
                symbols=self.symbols,
                start_date=start_date,
                end_date=end_date,
            )

        prev_equity = self.initial_cash

        for cur_date in trading_dates:
            # 1. 포지션 손절 및 ATR 트레일링 익절 최우선 평가
            self._evaluate_exits(cur_date)

            # 2. 신규 시그널 및 비중 조절 (온보딩 / 스윙 / 밴드 상한 강제축소)
            self._evaluate_signals_and_weights(cur_date)

            # 3. 당일 종가 기준 일별 스냅샷 기록
            snapshot = self._record_daily_snapshot(cur_date, prev_equity)
            self.snapshots.append(snapshot)
            prev_equity = snapshot.total_equity

        final_equity = self.snapshots[-1].total_equity if self.snapshots else self.initial_cash
        total_return_pct = float((final_equity / self.initial_cash - 1.0) * 100.0)

        return BacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            total_return_pct=total_return_pct,
            daily_snapshots=self.snapshots,
            trades=self.trades,
            symbols=self.symbols,
            start_date=start_date,
            end_date=end_date,
        )

    def _get_bar_at(self, symbol: str, cur_date: str) -> Optional[pd.Series]:
        df = self.data.get(symbol)
        if df is None or df.empty:
            return None
        rows = df[df["date"] == pd.to_datetime(cur_date)]
        return rows.iloc[0] if not rows.empty else None

    def _get_historical_slice(self, symbol: str, cur_date: str) -> pd.DataFrame:
        df = self.data.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        return df[df["date"] <= pd.to_datetime(cur_date)].copy()

    def _evaluate_exits(self, cur_date: str) -> None:
        """손절(-7%)과 ATR 트레일링 익절을 평가하여 트리거 시 당일 청산."""
        symbols_held = list(self.positions.keys())

        for symbol in symbols_held:
            pos = self.positions[symbol]
            bar = self._get_bar_at(symbol, cur_date)
            if bar is None:
                continue

            low = float(bar["low"])
            high = float(bar["high"])
            open_p = float(bar["open"])
            close_p = float(bar["close"])

            # ATR 계산을 위한 슬라이스
            hist = self._get_historical_slice(symbol, cur_date)
            atr_pct = None
            if len(hist) >= 15:
                tr = pd.concat([
                    hist["high"] - hist["low"],
                    (hist["high"] - hist["close"].shift(1)).abs(),
                    (hist["low"] - hist["close"].shift(1)).abs(),
                ], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                if close_p > 0:
                    atr_pct = atr / close_p

            # 고점 갱신 (당일 장중 고가 반영)
            pos.peak_price = max(pos.peak_price, high)

            # 1. 손절 체크: 장중 최저가가 평단 대비 -7% 도달 여부
            if (low - pos.avg_price) / pos.avg_price <= -self.stop_loss_pct:
                exec_price = min(open_p, pos.avg_price * (1.0 - self.stop_loss_pct))
                exec_price = max(exec_price, low)
                self._execute_sell(symbol, pos.qty, exec_price, cur_date, "STOP_LOSS")
                continue

            # 2. 트레일링 익절 체크
            trail_pct, arm_pct = exit_guard.trailing_params(atr_pct)
            peak = max(pos.peak_price, pos.avg_price)
            if (peak - pos.avg_price) / pos.avg_price >= arm_pct:
                # 고점 대비 하락 여부
                if (low - peak) / peak <= -trail_pct:
                    exec_price = peak * (1.0 - trail_pct)
                    exec_price = max(exec_price, low)
                    self._execute_sell(symbol, pos.qty, exec_price, cur_date, "TRAILING_TAKE_PROFIT")
                    continue

    def _evaluate_signals_and_weights(self, cur_date: str) -> None:
        """기술적 시그널, 밴드 상한 강제축소, 분할 온보딩 매수 평가."""
        cur_total_equity = self._calculate_current_equity(cur_date)
        if cur_total_equity <= 0:
            return

        for symbol in self.symbols:
            bar = self._get_bar_at(symbol, cur_date)
            if bar is None:
                continue

            close_p = float(bar["close"])
            if close_p <= 0:
                continue

            pos = self.positions.get(symbol)
            cur_qty = pos.qty if pos else 0.0
            cur_pos_val = cur_qty * close_p
            cur_weight = cur_pos_val / cur_total_equity
            target_weight = self.target_weights.get(symbol, 0.0)

            # 1. 밴드 상한 강제 축소 (목표비중 + 5% + 5% 초과)
            band_ceiling = target_weight + settings.REBALANCE_BAND_PCT + signal_engine.BAND_CEILING_BUFFER_PCT
            if cur_weight > band_ceiling and cur_qty > 0:
                excess_val = (cur_weight - (target_weight + settings.REBALANCE_BAND_PCT)) * cur_total_equity
                trim_qty = min(cur_qty, float(int(excess_val / close_p)))
                if trim_qty > 0:
                    self._execute_sell(symbol, trim_qty, close_p, cur_date, "band_ceiling_forced_trim")
                    continue

            # 2. 기술적 지표 시그널 산출
            hist = self._get_historical_slice(symbol, cur_date)
            if len(hist) < indicators.MIN_BARS_REQUIRED:
                continue

            tech_sig = indicators.compute_technical_signal(hist)
            combined = signal_engine.combine_signals(tech_sig, None, None, None)
            direction = combined.get("direction", "HOLD")
            strength = float(combined.get("strength", 0.0))

            # 3. 분할 온보딩 매수 (저비중/신규 종목의 점진적 채우기)
            if settings.ONBOARDING_ENABLED and target_weight > 0 and cur_weight < (target_weight - settings.REBALANCE_BAND_PCT):
                if direction != "SELL":
                    gap_val = (target_weight - cur_weight) * cur_total_equity
                    daily_budget = (target_weight * cur_total_equity) / max(1, self.onboarding_days)
                    buy_val = min(gap_val, daily_budget, self.max_order_notional)
                    # 종목당 최대 비중 캡
                    max_allowed_val = (self.max_position_pct * cur_total_equity) - cur_pos_val
                    buy_val = min(buy_val, max_allowed_val, self.cash)

                    buy_qty = float(int(buy_val / close_p))
                    if buy_qty > 0:
                        self._execute_buy(symbol, buy_qty, close_p, cur_date, "onboarding_tranche")
                        continue

            # 4. 스윙 시그널 매수/매도
            if strength >= signal_engine.SWING_SIGNAL_THRESHOLD:
                if direction == "BUY":
                    # 신호 강도에 비례한 매수 금액 (최대 자산의 15%)
                    swing_budget = cur_total_equity * signal_engine.SWING_TRADE_MAX_EQUITY_FRACTION * strength
                    swing_budget = min(swing_budget, self.max_order_notional)
                    # 비중 캡 체크
                    max_allowed_val = (self.max_position_pct * cur_total_equity) - cur_pos_val
                    buy_val = min(swing_budget, max_allowed_val, self.cash)

                    buy_qty = float(int(buy_val / close_p))
                    if buy_qty > 0:
                        self._execute_buy(symbol, buy_qty, close_p, cur_date, f"swing_signal_buy (str={strength:.2f})")
                elif direction == "SELL" and cur_qty > 0:
                    sell_budget = cur_pos_val * strength
                    sell_qty = min(cur_qty, float(int(sell_budget / close_p)))
                    if sell_qty > 0:
                        self._execute_sell(symbol, sell_qty, close_p, cur_date, f"swing_signal_sell (str={strength:.2f})")

    def _execute_buy(self, symbol: str, qty: float, price: float, cur_date: str, reason: str) -> None:
        exec_price = price * (1.0 + self.slippage_pct)
        notional = exec_price * qty
        fee = notional * self.fee_rate
        total_cost = notional + fee

        if total_cost > self.cash:
            qty = float(int((self.cash / (1.0 + self.fee_rate)) / exec_price))
            if qty <= 0:
                return
            notional = exec_price * qty
            fee = notional * self.fee_rate
            total_cost = notional + fee

        self.cash -= total_cost

        if symbol in self.positions:
            pos = self.positions[symbol]
            old_qty = pos.qty
            new_qty = old_qty + qty
            pos.avg_price = ((pos.avg_price * old_qty) + notional) / new_qty
            pos.qty = new_qty
            pos.peak_price = max(pos.peak_price, exec_price)
        else:
            self.positions[symbol] = Position(
                symbol=symbol,
                qty=qty,
                avg_price=exec_price,
                peak_price=exec_price,
                entry_date=cur_date,
                entry_avg_price=exec_price,
            )

        self.trades.append(TradeRecord(
            symbol=symbol,
            date=cur_date,
            side="buy",
            price=exec_price,
            qty=qty,
            notional=notional,
            fee=fee,
            tax=0.0,
            reason=reason,
        ))

    def _execute_sell(self, symbol: str, qty: float, price: float, cur_date: str, reason: str) -> None:
        if symbol not in self.positions or qty <= 0:
            return

        pos = self.positions[symbol]
        qty = min(qty, pos.qty)
        exec_price = price * (1.0 - self.slippage_pct)
        notional = exec_price * qty
        fee = notional * self.fee_rate
        tax = notional * self.tax_rate
        net_proceeds = notional - fee - tax

        self.cash += net_proceeds
        cost_basis = pos.avg_price * qty
        realized_pnl = net_proceeds - cost_basis
        ret_pct = ((net_proceeds / cost_basis) - 1.0) * 100.0 if cost_basis > 0 else 0.0

        pos.qty -= qty
        if pos.qty <= 0.0001:
            del self.positions[symbol]

        self.trades.append(TradeRecord(
            symbol=symbol,
            date=cur_date,
            side="sell",
            price=exec_price,
            qty=qty,
            notional=notional,
            fee=fee,
            tax=tax,
            reason=reason,
            realized_pnl=realized_pnl,
            return_pct=ret_pct,
        ))

    def _calculate_current_equity(self, cur_date: str) -> float:
        equity = self.cash
        for sym, pos in self.positions.items():
            bar = self._get_bar_at(sym, cur_date)
            p = float(bar["close"]) if bar is not None else pos.avg_price
            equity += pos.qty * p
        return equity

    def _record_daily_snapshot(self, cur_date: str, prev_equity: float) -> DailySnapshot:
        pos_val = 0.0
        pos_dict = {}
        for sym, pos in self.positions.items():
            bar = self._get_bar_at(sym, cur_date)
            p = float(bar["close"]) if bar is not None else pos.avg_price
            val = pos.qty * p
            pos_val += val
            pos_dict[sym] = {"qty": pos.qty, "price": p, "value": val}

        total_equity = self.cash + pos_val
        daily_ret = ((total_equity / prev_equity) - 1.0) if prev_equity > 0 else 0.0

        return DailySnapshot(
            date=cur_date,
            cash=self.cash,
            positions_value=pos_val,
            total_equity=total_equity,
            daily_return=daily_ret,
            positions=pos_dict,
        )
