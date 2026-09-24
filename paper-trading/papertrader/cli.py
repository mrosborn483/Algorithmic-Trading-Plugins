"""Command line entry point: python -m papertrader <command>

`run` is a paper-only scheduler. Live execution is intentionally NOT included.
"""
import argparse
import logging
import sys

import pandas as pd

from . import config as conf
from . import data, report
from .engine import backtest, paper_step
from .notify import Notifier, fmt_price, format_event
from .safety import LiveTradingLocked
from .scheduler import Scheduler, install_signal_handlers, scan_lock
from .strategies import REGISTRY

log = logging.getLogger("papertrader")


def _pairs(cfg, strategy=None, market=None):
    for inst, source in conf.instruments(cfg, market):
        for strat, markets in conf.strategies(cfg, strategy):
            if markets and inst.asset_class not in markets:
                continue
            yield inst, source, strat


def _load_frames(cfg, market, period=None):
    frames = {}
    for inst, source in conf.instruments(cfg, market):
        try:
            frames[inst.symbol] = data.fetch(inst.symbol, inst.timeframe, source, period=period)
        except Exception as exc:  # keep going if one feed fails
            log.error("data for %s failed: %s", inst.symbol, exc)
            frames[inst.symbol] = pd.DataFrame()
    return frames


def cmd_scan(cfg, args):
    with scan_lock(cfg.get("database", "data/paper.db")) as got_lock:
        if not got_lock:
            print("another scan is running - skipped")
            return 0
        return _scan(cfg, args)


def _scan(cfg, args):
    broker = conf.make_broker(cfg)
    tg = Notifier(dry_run=args.dry_run)
    notify = cfg.get("telegram", {}).get("notify", {})
    frames = _load_frames(cfg, args.market)
    failed = [s for s, f in frames.items() if f.empty]
    n_events = 0
    for inst, _, strat in _pairs(cfg, args.strategy, args.market):
        for ev in paper_step(broker, strat, inst, frames[inst.symbol]):
            n_events += 1
            if ev.kind == "entry" and notify.get("entries", True):
                tg.send(format_event(ev))
            elif ev.kind == "exit" and notify.get("exits", True):
                line = report.account_line(broker.store.trades(), ev.trade.account, broker.starting_balance)
                tg.send(format_event(ev, line))
    if failed and notify.get("errors", True):
        tg.send("⚠️ Data unavailable for: " + ", ".join(failed))
    print(f"scan complete: {n_events} event(s), {len(broker.store.positions())} open paper position(s)")


def cmd_positions(cfg, args):
    broker = conf.make_broker(cfg)
    positions = broker.store.positions()
    if not positions:
        print("no open paper positions")
    for p in positions:
        print(f"{p.strategy:<18} {p.symbol:<10} {'LONG ' if p.side == 1 else 'SHORT'} entry {fmt_price(p.entry_price)} "
              f"stop {fmt_price(p.stop)} target {fmt_price(p.target)} since {p.entry_time}")


def _board_text(trades, cfg, title, by):
    board = report.leaderboard(trades, float(cfg.get("starting_balance", 10_000)), by=by,
                               promotion=cfg.get("promotion"))
    return board, report.format_leaderboard(board, title)


def cmd_report(cfg, args):
    broker = conf.make_broker(cfg)
    trades = broker.store.trades()
    by = ("strategy", "asset_class") if args.by == "market" else ("strategy", "asset_class", "symbol") \
        if args.by == "symbol" else ("strategy",)
    board, text = _board_text(trades, cfg, "Paper trading leaderboard", by)
    text += f"\nOpen positions: {len(broker.store.positions())}"
    print(text.replace("<pre>", "").replace("</pre>", "").replace("<b>", "").replace("</b>", ""))
    if args.csv:
        board.to_csv(args.csv, index=False)
        trades.to_csv(args.csv.replace(".csv", "_trades.csv"), index=False)
        print(f"wrote {args.csv}")
    if args.send:
        Notifier().send(text)


def cmd_backtest(cfg, args):
    broker = conf.make_broker(cfg, db_path=":memory:")
    frames = _load_frames(cfg, args.market, period=args.period)
    for symbol, df in frames.items():
        if 0 < len(df) <= 250:
            log.warning("skipping %s: only %d bars (need > 250)", symbol, len(df))
    for inst, _, strat in _pairs(cfg, args.strategy, args.market):
        df = frames[inst.symbol]
        if len(df) > 250:
            backtest(broker, strat, inst, df)
    trades = broker.store.trades()
    board, text = _board_text(trades, cfg, "Backtest leaderboard", ("strategy", "asset_class"))
    print(text.replace("<pre>", "").replace("</pre>", "").replace("<b>", "").replace("</b>", ""))
    if args.csv:
        trades.to_csv(args.csv, index=False)
        print(f"wrote {args.csv}")
    if args.send:
        Notifier().send(text)


def cmd_run(cfg, args):
    """Paper-only scheduler: scan every interval, daily leaderboard to Telegram."""
    broker = conf.make_broker(cfg)
    tg = Notifier(dry_run=args.dry_run)
    scan_args = argparse.Namespace(market=None, strategy=None, dry_run=args.dry_run)
    report_args = argparse.Namespace(by="market", csv=None, send=not args.dry_run)

    sched = Scheduler(
        load_config=lambda: conf.load(args.config),
        scan=lambda c: cmd_scan(c, scan_args),
        send_report=lambda c: cmd_report(c, report_args),
        notify=tg.send,
        get_state=broker.store.get_state,
        set_state=broker.store.set_state,
    )
    install_signal_handlers(sched)
    sched.run(max_runs=args.max_runs, run_now=not args.wait)


def cmd_strategies(cfg, args):
    for name, cls in REGISTRY.items():
        print(f"{name:<18} {cls.description}")
        print(f"{'':<18} params: {{**{cls.base_params}, **{cls.default_params}}}")


def cmd_telegram_test(cfg, args):
    tg = Notifier()
    if tg.dry_run:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - see README 'Telegram setup'")
        return 1
    ok = tg.send("🧪 Paper trader connected. You will get PAPER entries, WIN/LOSS exits and leaderboards here.")
    print("sent" if ok else "failed - check token/chat id")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="papertrader", description="Multi-asset paper trading (live trading locked)")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="one pass: fetch closed bars, manage paper trades, send alerts")
    p.add_argument("--market", choices=["crypto", "stocks", "fx"])
    p.add_argument("--strategy")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Telegram")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("backtest", help="replay history for every strategy x market and rank them")
    p.add_argument("--market", choices=["crypto", "stocks", "fx"])
    p.add_argument("--strategy")
    p.add_argument("--period", help="yfinance period override, e.g. 2y, 729d")
    p.add_argument("--csv")
    p.add_argument("--send", action="store_true", help="send the leaderboard to Telegram")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("report", help="paper-trading leaderboard (wins/losses per strategy)")
    p.add_argument("--by", choices=["strategy", "market", "symbol"], default="market")
    p.add_argument("--csv")
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("run", help="PAPER-ONLY scheduler: scan every hour (config: schedule) + daily report")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Telegram")
    p.add_argument("--wait", action="store_true", help="wait for the next slot instead of scanning immediately")
    p.add_argument("--max-runs", type=int, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_run)

    sub.add_parser("positions", help="list open paper positions").set_defaults(fn=cmd_positions)
    sub.add_parser("strategies", help="list available strategies").set_defaults(fn=cmd_strategies)
    sub.add_parser("telegram-test", help="send a test Telegram message").set_defaults(fn=cmd_telegram_test)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = {} if args.cmd == "strategies" else conf.load(args.config)
        return args.fn(cfg, args) or 0
    except LiveTradingLocked as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
