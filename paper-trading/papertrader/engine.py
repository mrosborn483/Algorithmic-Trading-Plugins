"""Trading logic shared by the backtester and the paper scanner, so both behave identically."""
from dataclasses import dataclass, field

import pandas as pd

from .broker import PaperBroker
from .store import Position, Trade
from .strategies import Strategy


@dataclass
class Event:
    kind: str  # "entry" | "exit"
    position: Position | None = None
    trade: Trade | None = None


@dataclass
class Instrument:
    symbol: str
    asset_class: str
    timeframe: str
    allow_short: bool = True


@dataclass
class Runner:
    broker: PaperBroker
    strategy: Strategy
    inst: Instrument
    events: list[Event] = field(default_factory=list)

    def _position(self):
        return self.broker.store.open_position(self.strategy.name, self.inst.symbol, self.inst.timeframe)

    def step(self, time, bar, allow_entry: bool = True):
        """Process one *closed* bar that already carries entry/exit/atr columns."""
        pos = self._position()

        if pos is not None:
            closed = False
            if str(time) > pos.entry_time:  # the entry bar itself cannot hit the stop/target
                hit = self.broker.stop_or_target_hit(pos, bar)
                if hit:
                    self._close(pos, hit[0], time, hit[1])
                    closed = True
            if not closed:
                wants_out = bar["exit_long"] if pos.side == 1 else bar["exit_short"]
                if wants_out or bar["entry"] == -pos.side:
                    self._close(pos, bar["close"], time, "signal" if wants_out else "reverse")
                    closed = True
            if not closed:
                return

        side = int(bar["entry"])
        if not allow_entry or side == 0 or (side == -1 and not self.inst.allow_short) or pd.isna(bar["atr"]):
            return
        stop, target = self.strategy.stops(side, bar["close"], bar["atr"])
        pos = self.broker.open(self.strategy.name, self.inst.symbol, self.inst.asset_class, self.inst.timeframe,
                               side, bar["close"], stop, target, time)
        if pos:
            self.events.append(Event("entry", position=pos))

    def _close(self, pos, price, time, reason):
        trade = self.broker.close(pos, price, time, reason)
        self.events.append(Event("exit", trade=trade))


def backtest(broker: PaperBroker, strategy: Strategy, inst: Instrument, df: pd.DataFrame, close_open=True):
    """Replay history bar by bar. Signals are taken on bar close and filled at that close."""
    sig = strategy.signals(df)
    runner = Runner(broker, strategy, inst)
    for time, bar in sig.iterrows():
        runner.step(time, bar)
    if close_open:
        pos = runner._position()
        if pos is not None:
            runner._close(pos, sig["close"].iloc[-1], sig.index[-1], "end_of_test")
    return runner.events


def paper_step(broker: PaperBroker, strategy: Strategy, inst: Instrument, df: pd.DataFrame):
    """One live paper pass over the latest *closed* bars.

    * First run for this strategy/instrument: only the latest bar is evaluated (no fake history).
    * Later runs: every bar since the last scan is checked for stop/target/exit hits so nothing
      is missed between scans, but NEW entries are only taken on the latest bar (no stale fills).
    """
    if df.empty:
        return []
    store = broker.store
    key = f"last_bar:{strategy.name}:{inst.symbol}:{inst.timeframe}"
    last_seen = store.get_state(key)
    sig = strategy.signals(df)
    pending = sig[sig.index.astype(str) > last_seen] if last_seen else sig.iloc[-1:]
    runner = Runner(broker, strategy, inst)
    for i, (time, bar) in enumerate(pending.iterrows()):
        runner.step(time, bar, allow_entry=(i == len(pending) - 1))
    if len(pending):
        store.set_state(key, str(pending.index[-1]))
    return runner.events
