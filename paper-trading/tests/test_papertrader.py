import numpy as np
import pandas as pd
import pytest

from papertrader import report
from papertrader.broker import Costs, PaperBroker
from papertrader.cli import main
from papertrader.data import drop_incomplete
from papertrader.engine import Instrument, backtest, paper_step
from papertrader.indicators import rsi
from papertrader.notify import Notifier, format_event
from papertrader.safety import LiveTradingLocked, assert_paper_mode
from papertrader.store import Position, Store
from papertrader.strategies import REGISTRY, build


def synthetic(n=1500, seed=1, start="2025-01-01"):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)) + 0.15 * np.sin(t / 60))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.004, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.004, n))
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)


def make_broker(path=":memory:"):
    return PaperBroker(Store(path), 10_000, 1.0, {"crypto": Costs(10, 5)}, {"crypto": 2})


def test_rsi_bounds():
    r = rsi(synthetic()["close"]).dropna()
    assert r.between(0, 100).all()


@pytest.mark.parametrize("name", list(REGISTRY))
def test_every_strategy_backtests(name):
    broker = make_broker()
    backtest(broker, build(name), Instrument("BTC-USD", "crypto", "1h"), synthetic())
    trades = broker.store.trades()
    assert not broker.store.positions()
    assert len(trades) > 0
    # risk sizing: a stop-out should lose about 1R (plus costs)
    stops = trades[trades.exit_reason == "stop"]
    if len(stops):
        assert stops.r_multiple.between(-1.6, -0.9).all()


def test_stop_hit_first_when_bar_spans_both_levels():
    pos = Position("a", "s", "X", "crypto", "1h", 1, 1, 100, "t0", stop=95, target=110, risk_amount=5, entry_fee=0)
    bar = {"open": 100, "high": 111, "low": 94}
    assert PaperBroker.stop_or_target_hit(pos, bar) == (95, "stop")
    gap = {"open": 90, "high": 92, "low": 89}
    assert PaperBroker.stop_or_target_hit(pos, gap) == (90, "stop")


def test_short_disallowed():
    broker = make_broker()
    backtest(broker, build("donchian_breakout"), Instrument("SPY", "crypto", "1h", allow_short=False), synthetic())
    assert (broker.store.trades().side == 1).all()


def test_paper_step_first_run_only_latest_bar_and_no_duplicates():
    broker = make_broker()
    strat, inst, df = build("ema_crossover"), Instrument("BTC-USD", "crypto", "1h"), synthetic()
    paper_step(broker, strat, inst, df.iloc[:1000])
    assert broker.store.trades().empty  # history is not replayed as fake paper trades
    # scanning the same data again produces nothing new
    assert paper_step(broker, strat, inst, df.iloc[:1000]) == []
    # walking forward bar by bar produces trades
    events = []
    for i in range(1001, len(df) + 1):
        events += paper_step(broker, strat, inst, df.iloc[:i])
    assert any(e.kind == "entry" for e in events)
    assert any(e.kind == "exit" for e in events)


def test_paper_step_catches_stop_between_scans_and_skips_stale_entries():
    df = synthetic()
    strat, inst = build("bollinger_bounce"), Instrument("BTC-USD", "crypto", "1h")
    broker = make_broker()
    paper_step(broker, strat, inst, df.iloc[:1000])
    # open a long by hand, then crash price on a bar between scans and recover by the next scan
    t0 = df.index[999]
    broker.open(strat.name, "BTC-USD", "crypto", "1h", 1, df.close.iloc[999], df.close.iloc[999] * 0.95,
                df.close.iloc[999] * 1.5, t0)
    later = df.iloc[:1010].copy()
    later.iloc[1004, later.columns.get_loc("low")] = df.close.iloc[999] * 0.90
    events = paper_step(broker, strat, inst, later)
    exits = [e.trade for e in events if e.kind == "exit"]
    assert exits and exits[0].exit_reason == "stop" and exits[0].exit_time == str(df.index[1004])
    # new entries can only happen on the latest bar of a catch-up scan
    assert all(e.position.entry_time == str(later.index[-1]) for e in events if e.kind == "entry")


def test_cli_end_to_end(tmp_path, capsys):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    df = synthetic(1500, start="2020-01-01")
    df.to_csv(csv_dir / "BTC-USD.csv")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(open("config.example.yaml").read()
                   .replace("database: data/paper.db", f"database: {tmp_path / 'paper.db'}")
                   .replace("source: yfinance           #", f"source: csv:{csv_dir}  #")
                   .replace("symbols: [BTC-USD, ETH-USD, SOL-USD, XRP-USD]", "symbols: [BTC-USD]"))
    assert main(["-c", str(cfg), "backtest", "--market", "crypto"]) == 0
    assert "Backtest leaderboard" in capsys.readouterr().out
    assert main(["-c", str(cfg), "scan", "--market", "crypto", "--dry-run"]) == 0
    assert "scan complete" in capsys.readouterr().out
    assert main(["-c", str(cfg), "report"]) == 0
    assert main(["-c", str(cfg), "positions"]) == 0


def test_drop_incomplete():
    df = synthetic(10)
    now = df.index[-1] + pd.Timedelta("30min")
    assert len(drop_incomplete(df, "1h", now)) == 9
    assert len(drop_incomplete(df, "1h", df.index[-1] + pd.Timedelta("1h"))) == 10


def test_live_mode_refused(tmp_path):
    with pytest.raises(LiveTradingLocked):
        assert_paper_mode({"mode": "live"})
    assert_paper_mode({"mode": "paper"})
    cfg = tmp_path / "c.yaml"
    cfg.write_text("mode: live\n")
    assert main(["-c", str(cfg), "positions"]) == 2


def test_messages_and_leaderboard():
    broker = make_broker()
    events = backtest(broker, build("macd_trend"), Instrument("ETH-USD", "crypto", "1h"), synthetic())
    tg = Notifier(dry_run=True)
    for ev in events[:4]:
        tg.send(format_event(ev))
    assert any("PAPER" in m for m in tg.sent)
    assert any(("WIN" in m) or ("LOSS" in m) for m in tg.sent)
    board = report.leaderboard(broker.store.trades(), 10_000, promotion={"min_trades": 1})
    text = report.format_leaderboard(board, "Test")
    assert "macd_trend" in text and "<pre>" in text


def test_stats_math():
    trades = pd.DataFrame({"pnl": [200.0, -100.0, -100.0, 300.0], "r_multiple": [2, -1, -1, 3]})
    s = report.stats(trades, 1000)
    assert s["win_rate"] == 0.5
    assert s["profit_factor"] == pytest.approx(2.5)
    assert s["expectancy_r"] == pytest.approx(0.75)
    assert s["max_drawdown_pct"] == pytest.approx(200 / 1200 * 100)


# -- scheduler -------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

from papertrader.scheduler import Scheduler, next_run, report_due, scan_lock  # noqa: E402


def _dt(h, m, s=0):
    return datetime(2026, 9, 24, h, m, s, tzinfo=timezone.utc)


def test_next_run_aligned_to_hour_plus_offset():
    assert next_run(_dt(10, 0, 30), 60, 120) == _dt(10, 2)
    assert next_run(_dt(10, 2, 0), 60, 120) == _dt(11, 2)
    assert next_run(_dt(10, 59), 60, 120) == _dt(11, 2)
    assert next_run(_dt(10, 16), 15, 0) == _dt(10, 30)


def test_report_due_once_per_day():
    assert not report_due(_dt(20, 59), "21:00", None)
    assert report_due(_dt(21, 5), "21:00", None)
    assert not report_due(_dt(21, 5), "21:00", "2026-09-24")
    assert not report_due(_dt(21, 5), None, None)


class FakeClock:
    def __init__(self):
        self.now = _dt(20, 30)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now = self.now + pd.Timedelta(seconds=seconds).to_pytimedelta()


def _scheduler(cfgs, clock, scans, reports, msgs, state):
    it = iter(cfgs)
    last = {}

    def load():
        last["cfg"] = next(it, last.get("cfg"))
        if last["cfg"].get("mode") != "paper":
            raise LiveTradingLocked("locked")
        return last["cfg"]

    return Scheduler(load, lambda c: scans.append(clock()), lambda c: reports.append(clock()), msgs.append,
                     state.get, state.__setitem__, clock=clock, sleep=clock.sleep)


def test_scheduler_runs_hourly_and_reports_daily():
    clock, scans, reports, msgs, state = FakeClock(), [], [], [], {}
    cfg = {"mode": "paper", "schedule": {"interval_minutes": 60, "offset_seconds": 120, "daily_report_utc": "21:00"}}
    _scheduler([cfg], clock, scans, reports, msgs, state).run(max_runs=5)
    assert scans == [_dt(20, 30), _dt(21, 2), _dt(22, 2), _dt(23, 2), datetime(2026, 9, 25, 0, 2, tzinfo=timezone.utc)]
    assert reports == [_dt(21, 2)]  # once, not every hour after 21:00
    assert "PAPER ONLY" in msgs[0]


def test_scheduler_stops_if_config_switched_off_paper():
    clock, scans, reports, msgs, state = FakeClock(), [], [], [], {}
    paper = {"mode": "paper", "schedule": {}}
    s = _scheduler([paper, paper, {"mode": "live"}], clock, scans, reports, msgs, state)
    with pytest.raises(LiveTradingLocked):
        s.run(max_runs=10)
    assert len(scans) == 1
    assert "Scheduler stopped" in msgs[-1]


def test_scheduler_survives_scan_errors():
    clock, msgs, state = FakeClock(), [], {}
    calls = []

    def flaky(cfg):
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("feed down")

    s = Scheduler(lambda: {"mode": "paper", "schedule": {"daily_report_utc": None}}, flaky, lambda c: None,
                  msgs.append, state.get, state.__setitem__, clock=clock, sleep=clock.sleep)
    s.run(max_runs=4)
    assert len(calls) == 4
    assert sum("failed" in m for m in msgs) == 1  # alerted once, not every hour


def test_scan_lock_blocks_overlap(tmp_path):
    db = str(tmp_path / "paper.db")
    with scan_lock(db) as a:
        with scan_lock(db) as b:
            assert a and not b
    with scan_lock(db) as c:
        assert c


def test_cli_run_scheduler_one_pass(tmp_path, capsys):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    synthetic(600, start="2020-01-01").to_csv(csv_dir / "BTC-USD.csv")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""mode: paper
database: {tmp_path / 'paper.db'}
markets:
  crypto: {{source: "csv:{csv_dir}", timeframe: 1h, symbols: [BTC-USD]}}
strategies:
  ema_crossover: {{}}
""")
    assert main(["-c", str(cfg), "run", "--dry-run", "--max-runs", "1"]) == 0
    out = capsys.readouterr().out
    assert "PAPER ONLY" in out and "scan complete" in out


# -- strategy variants, window backtest, sweep -------------------------------
from types import SimpleNamespace  # noqa: E402

from papertrader import config as conf_mod  # noqa: E402
from papertrader import sweep  # noqa: E402
from papertrader.ai import AIReviewer, ai_scorecard, build_context  # noqa: E402


def test_config_variants_of_same_strategy():
    cfg = {"strategies": {"ema_fast": {"type": "ema_crossover", "params": {"fast": 5, "slow": 13}},
                          "ema_crossover": {}}}
    strats = [s for s, _ in conf_mod.strategies(cfg)]
    assert [s.label for s in strats] == ["ema_fast", "ema_crossover"]
    assert strats[0].params["fast"] == 5 and strats[1].params["fast"] == 9
    broker = make_broker()
    df = synthetic()
    for s in strats:
        backtest(broker, s, Instrument("BTC-USD", "crypto", "1h"), df)
    assert set(broker.store.trades().strategy) == {"ema_fast", "ema_crossover"}


def test_backtest_window_only_trades_recent_days():
    broker = make_broker()
    df = synthetic(2000)
    start = df.index[-1] - pd.Timedelta(days=7)
    backtest(broker, build("bollinger_bounce"), Instrument("BTC-USD", "crypto", "1h"), df, start=start)
    trades = broker.store.trades()
    assert len(trades) > 0
    assert (pd.to_datetime(trades.entry_time) >= start).all()


def test_sweep_variants_are_valid():
    for name in REGISTRY:
        vs = sweep.variants(name)
        assert vs, name
        for p in vs:
            build(name, p)  # constructs without error
    assert all(p["fast"] < p["slow"] for p in sweep.variants("ema_crossover"))
    assert all(p["exit_period"] < p["entry_period"] for p in sweep.variants("donchian_breakout"))


@pytest.mark.parametrize("workers", [1, 2])
def test_sweep_ranks_and_writes_config(workers):
    cfg = {"mode": "paper", "starting_balance": 10000,
           "markets": {"crypto": {"timeframe": "1h", "symbols": ["A", "B"]}}}
    frames = {"A": synthetic(3000, seed=3), "B": synthetic(3000, seed=4)}
    res = sweep.run_sweep(cfg, frames, days=14, compare_days=90, strategy="donchian_breakout", workers=workers)
    assert len(res) == len(sweep.variants("donchian_breakout"))
    ranked = sweep.rank(res, min_trades=1)
    text = sweep.format_ranked(ranked, 14, 90)
    assert "crypto" in text
    snippet = sweep.recommended_config(ranked)
    import yaml
    parsed = yaml.safe_load(snippet)["strategies"] or {}
    for label, spec in parsed.items():
        assert spec["type"] == "donchian_breakout" and spec["markets"] == ["crypto"]
        build(spec["type"], spec["params"], label=label)


# -- AI reviewer ---------------------------------------------------------------
class FakeClient:
    def __init__(self, decision="take", confidence=0.8, stop_reason="end_turn", fail=False):
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                if fail:
                    raise ConnectionError("down")
                text = '{"decision": "%s", "confidence": %s, "reasoning": "trend aligned"}' % (decision, confidence)
                return SimpleNamespace(stop_reason=stop_reason, model=kw["model"],
                                       content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)])

        self.beta = SimpleNamespace(messages=Messages())


def _walk_with_ai(client, settings=None, strategy="bollinger_bounce", bars=(1000, 1400)):
    broker = make_broker()
    reviewer = AIReviewer(broker.store, {"enabled": True, **(settings or {})}, 10_000, client=client)
    strat, inst, df = build(strategy), Instrument("BTC-USD", "crypto", "1h"), synthetic()
    events = []
    for i in range(*bars):
        events += paper_step(broker, strat, inst, df.iloc[:i], reviewer=reviewer)
    return broker, events


def test_ai_twin_takes_approved_trades_and_request_shape():
    client = FakeClient("take", 0.9)
    broker, events = _walk_with_ai(client)
    trades = broker.store.trades()
    assert set(trades.strategy) == {"bollinger_bounce", "bollinger_bounce+ai"}
    # one API call per signal, shared by both accounts
    plain_entries = [e for e in events if e.kind == "entry" and e.position.strategy == "bollinger_bounce"]
    assert len(client.calls) == len(plain_entries) > 0
    assert all("🤖 AI: TAKE" in e.note for e in plain_entries)
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "BTC-USD" in kw["messages"][0]["content"]
    assert len(broker.store.ai_decisions()) == len(client.calls)


def test_ai_skip_low_confidence_refusal_and_outage_block_only_the_twin():
    for client in (FakeClient("skip", 0.9), FakeClient("take", 0.3), FakeClient(stop_reason="refusal"),
                   FakeClient(fail=True)):
        broker, events = _walk_with_ai(client)
        strategies = set(broker.store.trades().strategy)
        assert strategies == {"bollinger_bounce"}, client
        assert any(e.kind == "skip" for e in events)


def test_ai_daily_budget_caps_calls():
    client = FakeClient("take", 0.9)
    _walk_with_ai(client, {"max_calls_per_day": 2})
    assert len(client.calls) == 2


def test_ai_scorecard_and_context():
    broker, _ = _walk_with_ai(FakeClient("take", 0.9))
    card = ai_scorecard(broker.store)
    assert "AI take" in card.index and card.loc["AI take", "signals"] > 0
    df = build("macd_trend").signals(synthetic())
    bar = df.iloc[-1]
    ctx = build_context(build("macd_trend"), Instrument("EURUSD=X", "fx", "1h"), df.index[-1], bar, 1,
                        bar.close * 0.99, bar.close * 1.02, df, "no trades yet")
    assert "EURUSD=X" in ctx and "RSI(14)" in ctx and "Last 30 bars" in ctx and "nan" not in ctx.lower()
