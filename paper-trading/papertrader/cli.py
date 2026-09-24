"""Command line entry point: python -m papertrader <command>

`run` is a paper-only scheduler. Live execution is intentionally NOT included.
"""
import argparse
import logging
import sys

import pandas as pd

from . import config as conf
from . import data, report
from .engine import AI_SUFFIX, backtest, paper_step
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


def _history_period(cfg, market, days):
    """yfinance period covering `days` plus indicator warm-up (200-bar EMAs etc.)."""
    tfs = {m.get("timeframe", "1h") for k, m in cfg.get("markets", {}).items() if not market or k == market}
    if tfs & {"5m", "15m", "30m"}:
        return "59d"
    if "1d" in tfs:
        return f"{days + 400}d"
    return f"{min(days + 60, 729)}d"


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
    reviewer = None
    if cfg.get("ai", {}).get("enabled") and not getattr(args, "no_ai", False):
        from .ai import AIReviewer

        reviewer = AIReviewer(broker.store, cfg.get("ai"), broker.starting_balance)
    frames = _load_frames(cfg, args.market)
    failed = [s for s, f in frames.items() if f.empty]
    n_events = 0
    for inst, _, strat in _pairs(cfg, args.strategy, args.market):
        use_ai = reviewer if reviewer is not None and reviewer.applies_to(strat) else None
        for ev in paper_step(broker, strat, inst, frames[inst.symbol], reviewer=use_ai):
            n_events += 1
            # the plain strategy's entry alert already carries the AI verdict, so the twin's entry is silent
            if ev.kind == "entry" and notify.get("entries", True) and not ev.position.strategy.endswith(AI_SUFFIX):
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
    if getattr(args, "ai", False):
        from .ai import ai_scorecard

        card = ai_scorecard(broker.store)
        ai_text = "\n🤖 AI scorecard (outcome of the plain strategy's trade, by AI verdict):\n"
        ai_text += card.to_string(float_format=lambda x: f"{x:,.2f}") if not card.empty else "no reviewed trades closed yet"
        print(ai_text)
        text += "\n<pre>" + ai_text + "</pre>"
    if args.send:
        Notifier().send(text)


def cmd_backtest(cfg, args):
    broker = conf.make_broker(cfg, db_path=":memory:")
    period = args.period or (_history_period(cfg, args.market, args.days) if args.days else None)
    frames = _load_frames(cfg, args.market, period=period)
    start = None
    if args.days:
        latest = max((df.index[-1] for df in frames.values() if not df.empty), default=None)
        start = latest - pd.Timedelta(days=args.days) if latest is not None else None
    for symbol, df in frames.items():
        if 0 < len(df) <= 250:
            log.warning("skipping %s: only %d bars (need > 250)", symbol, len(df))
    for inst, _, strat in _pairs(cfg, args.strategy, args.market):
        df = frames[inst.symbol]
        if len(df) > 250:
            backtest(broker, strat, inst, df, start=start)
    trades = broker.store.trades()
    title = f"Backtest leaderboard - last {args.days} days" if args.days else "Backtest leaderboard"
    board, text = _board_text(trades, cfg, title, ("strategy", "asset_class"))
    print(text.replace("<pre>", "").replace("</pre>", "").replace("<b>", "").replace("</b>", ""))
    if args.csv:
        trades.to_csv(args.csv, index=False)
        print(f"wrote {args.csv}")
    if args.send:
        Notifier().send(text)


def cmd_sweep(cfg, args):
    from . import sweep

    frames = _load_frames(cfg, args.market, period=_history_period(cfg, args.market, max(args.days, args.compare_days)))
    results = sweep.run_sweep(cfg, frames, args.days, args.compare_days, args.market, args.strategy, args.workers)
    ranked = sweep.rank(results, args.min_trades)
    text = sweep.format_ranked(ranked, args.days, args.compare_days, args.top)
    print(text.replace("<pre>", "").replace("</pre>", "").replace("<b>", "").replace("</b>", ""))
    print(f"\n{len(results)} strategy/setting/market combinations tested")
    if args.csv:
        ranked.to_csv(args.csv, index=False)
        print(f"wrote {args.csv}")
    if args.write:
        with open(args.write, "w") as fh:
            fh.write(sweep.recommended_config(ranked, args.per_market))
        print(f"wrote recommended strategies to {args.write}")
    if args.send:
        Notifier().send(text)


def cmd_run(cfg, args):
    """Paper-only scheduler: scan every interval, daily leaderboard to Telegram."""
    broker = conf.make_broker(cfg)
    tg = Notifier(dry_run=args.dry_run)
    scan_args = argparse.Namespace(market=None, strategy=None, dry_run=args.dry_run, no_ai=False)
    report_args = argparse.Namespace(by="market", csv=None, send=not args.dry_run,
                                     ai=bool(cfg.get("ai", {}).get("enabled")))

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
    p.add_argument("--no-ai", action="store_true", help="skip AI reviews for this scan")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("sweep", help="try every strategy x setting x market over the last N days and rank them")
    p.add_argument("--days", type=int, default=7, help="recent window to rank on (default 7)")
    p.add_argument("--compare-days", type=int, default=90, help="longer window used as a robustness check")
    p.add_argument("--market", choices=["crypto", "stocks", "fx"])
    p.add_argument("--strategy", choices=list(REGISTRY))
    p.add_argument("--min-trades", type=int, default=3, help="ignore variants with fewer trades in the window")
    p.add_argument("--top", type=int, default=5, help="rows to show per market")
    p.add_argument("--workers", type=int, help="parallel processes (default: all CPU cores)")
    p.add_argument("--write", metavar="FILE", help="write the best robust variants as a config snippet")
    p.add_argument("--per-market", type=int, default=3, help="variants per market in --write")
    p.add_argument("--csv")
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_sweep)

    p = sub.add_parser("backtest", help="replay history for every strategy x market and rank them")
    p.add_argument("--market", choices=["crypto", "stocks", "fx"])
    p.add_argument("--strategy")
    p.add_argument("--period", help="yfinance period override, e.g. 2y, 729d")
    p.add_argument("--days", type=int, help="only trade the last N days (indicators still warm up on older data)")
    p.add_argument("--csv")
    p.add_argument("--send", action="store_true", help="send the leaderboard to Telegram")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("report", help="paper-trading leaderboard (wins/losses per strategy)")
    p.add_argument("--by", choices=["strategy", "market", "symbol"], default="market")
    p.add_argument("--ai", action="store_true", help="add the AI scorecard (AI-approved vs AI-rejected outcomes)")
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
