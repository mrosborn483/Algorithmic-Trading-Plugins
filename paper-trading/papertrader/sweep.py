"""Strategy sweep: try every strategy with a grid of settings on every market, over a recent
window (default: the past 7 days), and check each result against a longer window
(default: 90 days) so that a lucky week is not mistaken for an edge.

Runs in parallel across all CPU cores.
"""
import itertools
import os
from concurrent.futures import ProcessPoolExecutor

import pandas as pd
import yaml

from . import config as conf
from . import report
from .engine import Instrument, backtest
from .strategies import REGISTRY, build


def variants(type_name: str) -> list[dict]:
    cls = REGISTRY[type_name]
    keys = list(cls.grid)
    out = []
    for combo in itertools.product(*(cls.grid[k] for k in keys)):
        p = dict(zip(keys, combo))
        merged = {**cls.default_params, **p}
        # skip nonsensical combinations
        if "fast" in merged and "slow" in merged and merged["fast"] >= merged["slow"]:
            continue
        if "exit_period" in merged and merged["exit_period"] >= merged["entry_period"]:
            continue
        for stop_atr, target_atr in cls.stop_target_grid:
            out.append({**p, "stop_atr": stop_atr, "target_atr": target_atr})
    return out


def variant_label(type_name: str, params: dict) -> str:
    short = {"stop_atr": "sl", "target_atr": "tp", "entry_period": "in", "exit_period": "out",
             "min_squeeze_bars": "sq", "oversold": "os", "overbought": "ob", "period": "p"}
    parts = [f"{short.get(k, k[:2])}{v:g}".replace(".", "_") if isinstance(v, float) else f"{short.get(k, k[:2])}{v}"
             for k, v in params.items()]
    return f"{type_name}_" + "_".join(parts)


def _run_window(cfg, type_name, params, pairs, start):
    broker = conf.make_broker(cfg, db_path=":memory:")
    strat = build(type_name, params, label=variant_label(type_name, params))
    for inst, df in pairs:
        if len(df) > 250 and df.index[-1] >= start:
            backtest(broker, strat, inst, df, start=start)
    return report.stats(broker.store.trades(), float(cfg.get("starting_balance", 10_000)))


def _job(job):
    cfg, type_name, params, market, pairs, recent_start, long_start = job
    recent = _run_window(cfg, type_name, params, pairs, recent_start)
    longer = _run_window(cfg, type_name, params, pairs, long_start)
    row = {"market": market, "strategy": type_name, "label": variant_label(type_name, params), "params": params}
    row.update({f"{k}_recent": v for k, v in recent.items()})
    row.update({f"{k}_long": v for k, v in longer.items()})
    return row


def run_sweep(cfg: dict, frames: dict[str, pd.DataFrame], days: int = 7, compare_days: int = 90,
              market: str | None = None, strategy: str | None = None, workers: int | None = None) -> pd.DataFrame:
    by_market: dict[str, list[tuple[Instrument, pd.DataFrame]]] = {}
    for inst, _ in conf.instruments(cfg, market):
        df = frames.get(inst.symbol)
        if df is not None and not df.empty:
            by_market.setdefault(inst.asset_class, []).append((inst, df))
    if not by_market:
        return pd.DataFrame()
    now = max(df.index[-1] for pairs in by_market.values() for _, df in pairs)
    recent_start = now - pd.Timedelta(days=days)
    long_start = now - pd.Timedelta(days=compare_days)

    types = [strategy] if strategy else list(REGISTRY)
    jobs = [(cfg, t, p, m, pairs, recent_start, long_start)
            for t in types for p in variants(t) for m, pairs in by_market.items()]
    workers = workers or os.cpu_count() or 1
    if workers == 1:
        rows = [_job(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(_job, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
    return pd.DataFrame(rows)


def rank(results: pd.DataFrame, min_trades: int = 3) -> pd.DataFrame:
    """Best recent performers first, but only those that also hold up over the longer window."""
    if results.empty:
        return results
    df = results[results["trades_recent"] >= min_trades].copy()
    df["robust"] = ((df["expectancy_r_long"] >= 0.10) & (df["profit_factor_long"] >= 1.2)
                    & (df["trades_long"] >= 10))
    return df.sort_values(["market", "robust", "expectancy_r_recent", "net_pnl_recent"],
                          ascending=[True, False, False, False])


def format_ranked(ranked: pd.DataFrame, days: int, compare_days: int, top: int = 5) -> str:
    if ranked.empty:
        return f"🔬 <b>Sweep ({days}d)</b>\nNot enough trades in the window to rank anything."
    lines = [f"🔬 <b>Best strategies - last {days} days</b> (checked against {compare_days} days)"]
    for mkt, grp in ranked.groupby("market", sort=False):
        lines += [f"\n<b>{mkt}</b>", "<pre>"]
        lines.append(f"{'variant':<41}{'n':>3}{'win%':>5}{'expR':>6}{'net$':>8} | {'n':>4}{'expR':>6}{'PF':>5}")
        for _, r in grp.head(top).iterrows():
            pf = "inf" if r["profit_factor_long"] == float("inf") else f"{r['profit_factor_long']:.2f}"
            mark = "✓" if r["robust"] else "✗"
            lines.append(
                f"{mark}{r['label'][:40]:<40}{r['trades_recent']:>3}{r['win_rate_recent'] * 100:>5.0f}"
                f"{r['expectancy_r_recent']:>6.2f}{r['net_pnl_recent']:>8,.0f} | "
                f"{r['trades_long']:>4}{r['expectancy_r_long']:>6.2f}{pf:>5}"
            )
        lines.append("</pre>")
    lines.append(f"Left = last {days}d, right = last {compare_days}d. ✓ = also holds up over {compare_days}d (≥0.10R, PF≥1.2). "
                 "One week is a small sample - prefer ✓ rows.")
    return "\n".join(lines)


def recommended_config(ranked: pd.DataFrame, per_market: int = 3) -> str:
    """A `strategies:` block for config.yaml holding the top robust variants of each market."""
    chosen = {}
    for mkt, grp in ranked[ranked["robust"]].groupby("market"):
        for _, r in grp.head(per_market).iterrows():
            label = f"{r['label']}_{mkt}"
            chosen[label] = {"type": r["strategy"], "markets": [mkt], "params": dict(r["params"])}
    header = "# Generated by `papertrader sweep`: paste under `strategies:` in config.yaml (or replace it)\n"

    class NoAliases(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    return header + yaml.dump({"strategies": chosen}, Dumper=NoAliases, sort_keys=False, default_flow_style=None)
