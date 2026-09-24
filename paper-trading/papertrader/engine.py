"""Trading logic shared by the backtester and the paper scanner, so both behave identically."""
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .broker import PaperBroker
from .store import Position, Trade
from .strategies import Strategy

AI_SUFFIX = "+ai"


@dataclass
class Event:
    kind: str  # "entry" | "exit" | "skip"
    position: Position | None = None
    trade: Trade | None = None
    note: str = ""


@dataclass
class Instrument:
    symbol: str
    asset_class: str
    timeframe: str
    allow_short: bool = True


# entry_filter(time, bar, side, stop, target) -> (take_trade, note)
EntryFilter = Callable[[pd.Timestamp, pd.Series, int, float, float], tuple[bool, str]]


@dataclass
class Runner:
    broker: PaperBroker
    strategy: Strategy
    inst: Instrument
    suffix: str = ""  # "+ai" for the AI-filtered twin account
    entry_filter: EntryFilter | None = None
    events: list[Event] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.strategy.label + self.suffix

    def _position(self):
        return self.broker.store.open_position(self.label, self.inst.symbol, self.inst.timeframe)

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
        note = ""
        if self.entry_filter is not None:
            take, note = self.entry_filter(time, bar, side, stop, target)
            if not take:
                self.events.append(Event("skip", note=note))
                return
        pos = self.broker.open(self.label, self.inst.symbol, self.inst.asset_class, self.inst.timeframe,
                               side, bar["close"], stop, target, time)
        if pos:
            self.events.append(Event("entry", position=pos, note=note))

    def _close(self, pos, price, time, reason):
        trade = self.broker.close(pos, price, time, reason)
        self.events.append(Event("exit", trade=trade))


def backtest(broker: PaperBroker, strategy: Strategy, inst: Instrument, df: pd.DataFrame, close_open=True,
             start: pd.Timestamp | None = None):
    """Replay history bar by bar. Signals are taken on bar close and filled at that close.

    With `start`, the full history is still used to warm up indicators but trading only
    happens from `start` onward (e.g. "how would this have done over the past week").
    """
    sig = strategy.signals(df)
    if start is not None:
        sig = sig[sig.index >= start]
    runner = Runner(broker, strategy, inst)
    for time, bar in sig.iterrows():
        runner.step(time, bar)
    if close_open and not sig.empty:
        pos = runner._position()
        if pos is not None:
            runner._close(pos, sig["close"].iloc[-1], sig.index[-1], "end_of_test")
    return runner.events


# reviewer(strategy, inst, time, bar, side, stop, target, history) -> (take_trade, note)
Reviewer = Callable[..., tuple[bool, str]]


def paper_step(broker: PaperBroker, strategy: Strategy, inst: Instrument, df: pd.DataFrame,
               reviewer: Reviewer | None = None):
    """One live paper pass over the latest *closed* bars.

    * First run for this strategy/instrument: only the latest bar is evaluated (no fake history).
    * Later runs: every bar since the last scan is checked for stop/target/exit hits so nothing
      is missed between scans, but NEW entries are only taken on the latest bar (no stale fills).
    * With a `reviewer` (the AI), a twin account "<strategy>+ai" runs next to the plain strategy
      and only takes the entries the reviewer approves. The plain strategy is unaffected, so
      the leaderboard shows directly whether the AI adds value.
    """
    if df.empty:
        return []
    store = broker.store
    key = f"last_bar:{strategy.label}:{inst.symbol}:{inst.timeframe}"
    last_seen = store.get_state(key)
    sig = strategy.signals(df)
    pending = sig[sig.index.astype(str) > last_seen] if last_seen else sig.iloc[-1:]

    runners = [Runner(broker, strategy, inst)]
    if reviewer is not None:
        verdicts = {}

        def decide(time, bar, side, stop, target):
            # one review per signal, shared by both accounts
            if (time, side) not in verdicts:
                verdicts[(time, side)] = reviewer(strategy, inst, time, bar, side, stop, target, sig.loc[:time])
            return verdicts[(time, side)]

        # plain strategy: always takes the trade, alert just shows the AI's opinion
        runners[0].entry_filter = lambda *a: (True, decide(*a)[1])
        runners.append(Runner(broker, strategy, inst, suffix=AI_SUFFIX, entry_filter=decide))

    for i, (time, bar) in enumerate(pending.iterrows()):
        for runner in runners:
            runner.step(time, bar, allow_entry=(i == len(pending) - 1))
    if len(pending):
        store.set_state(key, str(pending.index[-1]))
    return [ev for r in runners for ev in r.events]
