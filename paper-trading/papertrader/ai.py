"""AI trade reviewer (Claude). PAPER ONLY.

When a strategy fires, Claude gets a compact snapshot of the market (recent bars, indicators,
higher-timeframe trend, the strategy's own track record) and answers take/skip with a
confidence. It is a second opinion, not a signal generator:

* The plain strategy keeps trading every signal. A twin account "<strategy>+ai" only takes
  the trades Claude approves. The leaderboard compares the two, so you can see whether the
  AI actually adds value before trusting it.
* Every review is stored in the `ai_decisions` table, and `report --ai` compares AI-approved
  and AI-rejected signals by their real outcomes.
* If the API is unavailable, over budget or declines to answer, the twin skips the trade
  (fails closed). The plain strategy is never blocked.

It is deliberately NOT used in backtests: the model may already know how past prices moved,
which would make backtested AI results look better than they really are. Judge it on forward
paper trades only.

Requires ANTHROPIC_API_KEY (or another Anthropic credential the SDK can find) and
`pip install anthropic`.
"""
import json
import logging
from datetime import datetime, timezone

import pandas as pd

from . import indicators as ind
from . import report
from .engine import Instrument
from .store import Store
from .strategies import Strategy

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "model": "claude-opus-5",
    "effort": "medium",           # low | medium | high | xhigh | max
    "min_confidence": 0.55,       # the twin needs take AND at least this confidence
    "max_calls_per_day": 150,     # hard cost cap; after it the twin skips new signals
    "strategies": None,           # list of strategy labels to review, None = all
}

SYSTEM = """You review trade signals for a systematic PAPER-trading research project that covers \
crypto, stocks and FX. A rules-based strategy has just fired on a closed bar. Decide whether a \
disciplined discretionary trader would take this specific trade, or skip it.

Judge the setup, not the strategy in general:
- Does the higher-timeframe trend and recent price structure support the direction?
- Is volatility sensible for the stop distance, or is the stop likely to be hit by noise?
- Is the entry late, meaning it chases an extended move right into nearby support or resistance?
- Is this strategy type suited to the current regime (trending vs ranging)?
- The strategy's recent track record on this market, if any.

Skipping is not free: skipped winners cost as much as taken losers. Only skip when the context \
gives a concrete reason. Set confidence to your honest probability (0-1) that taking the trade is \
the better choice. Keep reasoning to 1-3 short sentences that name the deciding factors."""

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["take", "skip"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["decision", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _fmt(x) -> str:
    if pd.isna(x):
        return "n/a"
    ax = abs(x)
    return f"{x:,.2f}" if ax >= 100 else f"{x:.4f}" if ax >= 1 else f"{x:.6f}"


def build_context(strategy: Strategy, inst: Instrument, time, bar, side, stop, target,
                  history: pd.DataFrame, track_record: str) -> str:
    c = history["close"]
    price = bar["close"]
    atr = bar["atr"]
    ema20, ema50, ema200 = (ind.ema(c, n).iloc[-1] for n in (20, 50, 200))
    rsi = ind.rsi(c, 14).iloc[-1]
    mid, up, lo = ind.bollinger(c, 20, 2.0)
    pct_b = (price - lo.iloc[-1]) / (up.iloc[-1] - lo.iloc[-1]) if up.iloc[-1] != lo.iloc[-1] else float("nan")
    bw = ((up - lo) / mid).dropna()
    bw_pctile = (bw.rank(pct=True).iloc[-1] * 100) if len(bw) else float("nan")
    atr_series = ind.atr(history, 14)
    vol_regime = atr / atr_series.tail(100).mean() if len(atr_series.dropna()) else float("nan")

    def ret(n):
        return (price / c.iloc[-n - 1] - 1) * 100 if len(c) > n else float("nan")

    daily = history.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    recent = history.tail(30)[["open", "high", "low", "close"]]
    rr = abs(target - price) / abs(price - stop) if price != stop else float("nan")

    lines = [
        f"Instrument: {inst.symbol} ({inst.asset_class}), timeframe {inst.timeframe}, bar closed {time}",
        f"Strategy: {strategy.name} - {strategy.description}",
        f"Parameters: {json.dumps(strategy.params)}",
        f"Proposed: {'LONG' if side == 1 else 'SHORT'} at {_fmt(price)}, stop {_fmt(stop)}, target {_fmt(target)} "
        f"(reward:risk {rr:.2f}, stop = {abs(price - stop) / atr:.1f} ATR)",
        "",
        "Indicators on this bar:",
        f"- ATR(14) {_fmt(atr)} = {atr / price * 100:.2f}% of price; ATR vs its 100-bar average: {vol_regime:.2f}x",
        f"- RSI(14) {rsi:.1f}",
        f"- Price vs EMA20 {(price / ema20 - 1) * 100:+.2f}%, EMA50 {(price / ema50 - 1) * 100:+.2f}%, "
        f"EMA200 {(price / ema200 - 1) * 100:+.2f}%",
        f"- Bollinger %B {pct_b:.2f}, bandwidth percentile (whole sample) {bw_pctile:.0f}",
        f"- Returns: last 5 bars {ret(5):+.2f}%, 20 bars {ret(20):+.2f}%, 100 bars {ret(100):+.2f}%",
        "",
        "Daily closes (last 15 days): " + ", ".join(_fmt(x) for x in daily["close"].tail(15)),
        "",
        f"Last {len(recent)} bars (oldest first) as time,open,high,low,close:",
    ]
    lines += [f"{t:%m-%d %H:%M},{_fmt(r.open)},{_fmt(r.high)},{_fmt(r.low)},{_fmt(r.close)}"
              for t, r in recent.iterrows()]
    lines += ["", f"Track record: {track_record}"]
    return "\n".join(lines)


class AIReviewer:
    def __init__(self, store: Store, settings: dict, starting_balance: float, client=None):
        self.store = store
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.starting_balance = starting_balance
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def applies_to(self, strategy: Strategy) -> bool:
        only = self.cfg.get("strategies")
        return not only or strategy.label in only

    def _budget_ok(self) -> bool:
        key = f"ai_calls:{datetime.now(timezone.utc).date().isoformat()}"
        used = int(self.store.get_state(key) or 0)
        if used >= int(self.cfg["max_calls_per_day"]):
            return False
        self.store.set_state(key, str(used + 1))
        return True

    def _track_record(self, strategy: Strategy, inst: Instrument) -> str:
        trades = self.store.trades()
        mine = trades[(trades["strategy"] == strategy.label) & (trades["asset_class"] == inst.asset_class)]
        if mine.empty:
            return "no closed paper trades yet for this strategy on this market."
        s = report.stats(mine, self.starting_balance)
        sym = mine[mine["symbol"] == inst.symbol].tail(5)
        last = ", ".join(f"{r:+.1f}R" for r in sym["r_multiple"]) or "none"
        return (f"{s['trades']} paper trades on {inst.asset_class}: win rate {s['win_rate'] * 100:.0f}%, "
                f"expectancy {s['expectancy_r']:+.2f}R, profit factor {s['profit_factor']:.2f}. "
                f"Last trades on {inst.symbol}: {last}.")

    def ask(self, context: str) -> dict:
        response = self.client.beta.messages.create(
            model=self.cfg["model"],
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if the model declines, the API retries on Anthropic's recommended fallback
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
        if response.stop_reason == "refusal":
            return {"decision": "skip", "confidence": 0.0, "reasoning": "model declined to review", "model": response.model}
        text = next(b.text for b in response.content if b.type == "text")
        return {**json.loads(text), "model": response.model}

    def __call__(self, strategy: Strategy, inst: Instrument, time, bar, side, stop, target, history):
        """Returns (take_trade, note) for the engine."""
        if not self.applies_to(strategy):
            return True, ""
        if not self._budget_ok():
            return False, "🤖 AI: daily review budget used up - twin skipped"
        try:
            context = build_context(strategy, inst, time, bar, side, stop, target, history,
                                    self._track_record(strategy, inst))
            verdict = self.ask(context)
        except Exception as exc:  # network, auth, rate limit, bad JSON: fail closed for the twin only
            log.error("AI review failed for %s %s: %s", strategy.label, inst.symbol, exc)
            return False, f"🤖 AI: unavailable ({type(exc).__name__}) - twin skipped"
        confidence = max(0.0, min(1.0, float(verdict.get("confidence", 0))))
        take = verdict.get("decision") == "take" and confidence >= float(self.cfg["min_confidence"])
        self.store.add_ai_decision(
            created_at=datetime.now(timezone.utc).isoformat(), strategy=strategy.label, symbol=inst.symbol,
            timeframe=inst.timeframe, bar_time=str(time), side=side, take=int(take), confidence=confidence,
            reasoning=str(verdict.get("reasoning", ""))[:1000], model=verdict.get("model", self.cfg["model"]),
        )
        note = f"🤖 AI: {'TAKE' if take else 'SKIP'} ({confidence:.0%}) - {verdict.get('reasoning', '')}"
        return take, note


def ai_scorecard(store: Store) -> pd.DataFrame:
    """Did the AI's calls hold up? Joins each review with the plain strategy's actual trade."""
    dec = store.ai_decisions()
    trades = store.trades()
    if dec.empty or trades.empty:
        return pd.DataFrame()
    merged = dec.merge(trades, left_on=["strategy", "symbol", "bar_time"], right_on=["strategy", "symbol", "entry_time"])
    if merged.empty:
        return pd.DataFrame()
    merged["verdict"] = merged["take"].map({1: "AI take", 0: "AI skip"})
    g = merged.groupby("verdict")
    return pd.DataFrame({
        "signals": g.size(),
        "win_rate": g.apply(lambda x: (x["pnl"] > 0).mean(), include_groups=False),
        "avg_r": g["r_multiple"].mean(),
        "net_pnl": g["pnl"].sum(),
    })
