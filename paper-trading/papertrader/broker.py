"""Simulated (paper) broker: position sizing, fills with fees/slippage, stop & target handling."""
from dataclasses import dataclass

from .store import Position, Store, Trade


@dataclass
class Costs:
    fee_bps: float = 0.0  # per side, on notional
    slippage_bps: float = 0.0  # per side, applied against us


class PaperBroker:
    def __init__(self, store: Store, starting_balance: float, risk_per_trade_pct: float,
                 costs: dict[str, Costs], max_leverage: dict[str, float]):
        self.store = store
        self.starting_balance = starting_balance
        self.risk_pct = risk_per_trade_pct / 100
        self.costs = costs
        self.max_leverage = max_leverage

    @staticmethod
    def account_name(strategy: str, asset_class: str) -> str:
        return f"{strategy}:{asset_class}"

    def equity(self, account: str) -> float:
        return self.starting_balance + self.store.realized_pnl(account)

    def _costs(self, asset_class) -> Costs:
        return self.costs.get(asset_class, Costs())

    def open(self, strategy, symbol, asset_class, timeframe, side, price, stop, target, time) -> Position | None:
        c = self._costs(asset_class)
        fill = price * (1 + side * c.slippage_bps / 1e4)
        per_unit_risk = abs(fill - stop)
        if per_unit_risk <= 0 or (target - fill) * side <= 0:
            return None
        account = self.account_name(strategy, asset_class)
        equity = self.equity(account)
        if equity <= 0:
            return None
        qty = equity * self.risk_pct / per_unit_risk
        max_qty = equity * self.max_leverage.get(asset_class, 1.0) / fill
        qty = min(qty, max_qty)
        pos = Position(
            account=account, strategy=strategy, symbol=symbol, asset_class=asset_class, timeframe=timeframe,
            side=side, qty=qty, entry_price=fill, entry_time=str(time), stop=stop, target=target,
            risk_amount=qty * per_unit_risk, entry_fee=qty * fill * c.fee_bps / 1e4,
        )
        return self.store.add_position(pos)

    @staticmethod
    def stop_or_target_hit(pos: Position, bar) -> tuple[float, str] | None:
        """Check one OHLC bar. Gaps fill at the open; if both levels are inside the bar we
        assume the stop was hit first (conservative)."""
        o, h, l = bar["open"], bar["high"], bar["low"]
        if pos.side == 1:
            if l <= pos.stop:
                return min(o, pos.stop), "stop"
            if h >= pos.target:
                return max(o, pos.target), "target"
        else:
            if h >= pos.stop:
                return max(o, pos.stop), "stop"
            if l <= pos.target:
                return min(o, pos.target), "target"
        return None

    def close(self, pos: Position, price: float, time, reason: str, slip: bool = True) -> Trade:
        c = self._costs(pos.asset_class)
        fill = price * (1 - pos.side * c.slippage_bps / 1e4) if slip else price
        exit_fee = pos.qty * fill * c.fee_bps / 1e4
        gross = (fill - pos.entry_price) * pos.side * pos.qty
        fees = pos.entry_fee + exit_fee
        pnl = gross - fees
        trade = Trade(
            account=pos.account, strategy=pos.strategy, symbol=pos.symbol, asset_class=pos.asset_class,
            timeframe=pos.timeframe, side=pos.side, qty=pos.qty, entry_price=pos.entry_price,
            entry_time=pos.entry_time, exit_price=fill, exit_time=str(time), exit_reason=reason,
            pnl=pnl, fees=fees, r_multiple=pnl / pos.risk_amount if pos.risk_amount else 0.0,
            return_pct=(fill / pos.entry_price - 1) * pos.side * 100,
        )
        self.store.remove_position(pos.id)
        return self.store.add_trade(trade)
