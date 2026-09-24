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
