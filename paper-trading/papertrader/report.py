"""Performance stats and the strategy leaderboard used to pick what goes live."""
import numpy as np
import pandas as pd

DEFAULT_PROMOTION = {
    "min_trades": 30,
    "min_profit_factor": 1.3,
    "min_win_rate": 0.35,
    "min_expectancy_r": 0.10,
    "max_drawdown_pct": 15.0,
}


def stats(trades: pd.DataFrame, starting_balance: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0, "profit_factor": 0.0,
                "expectancy_r": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0, "max_drawdown_pct": 0.0,
                "return_pct": 0.0}
    pnl = trades["pnl"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_loss = -losses.sum()
    equity = starting_balance + pnl.cumsum()
    peak = np.maximum.accumulate(np.concatenate([[starting_balance], equity.values]))[1:]
    dd = ((peak - equity.values) / peak).max() * 100
    r = trades["r_multiple"].astype(float)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "net_pnl": pnl.sum(),
        "profit_factor": (wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "expectancy_r": r.mean(),
        "avg_win_r": r[pnl > 0].mean() if len(wins) else 0.0,
        "avg_loss_r": r[pnl <= 0].mean() if len(losses) else 0.0,
        "max_drawdown_pct": dd,
        "return_pct": pnl.sum() / starting_balance * 100,
    }


def leaderboard(trades: pd.DataFrame, starting_balance: float, by=("strategy", "asset_class"),
                promotion: dict | None = None) -> pd.DataFrame:
    crit = {**DEFAULT_PROMOTION, **(promotion or {})}
    if trades.empty:
        return pd.DataFrame()
    rows = []
    # Drawdown is measured per paper account (strategy x asset class) in trade order.
    for keys, grp in trades.sort_values(["exit_time", "id"]).groupby(list(by)):
        keys = keys if isinstance(keys, tuple) else (keys,)
        s = stats(grp, starting_balance)
        s["ready"] = (
            s["trades"] >= crit["min_trades"]
            and s["profit_factor"] >= crit["min_profit_factor"]
            and s["win_rate"] >= crit["min_win_rate"]
            and s["expectancy_r"] >= crit["min_expectancy_r"]
            and s["max_drawdown_pct"] <= crit["max_drawdown_pct"]
        )
        rows.append({**dict(zip(by, keys)), **s})
    df = pd.DataFrame(rows)
    return df.sort_values(["ready", "expectancy_r", "net_pnl"], ascending=False).reset_index(drop=True)


def format_leaderboard(board: pd.DataFrame, title: str, top: int = 20) -> str:
    if board.empty:
        return f"📊 <b>{title}</b>\nNo closed trades yet."
    lines = [f"📊 <b>{title}</b>", "<pre>"]
    lines.append(f"{'#':>2} {'strategy':<19}{'mkt':<7}{'n':>4}{'win%':>6}{'PF':>6}{'expR':>7}{'net$':>10}{'DD%':>6}")
    for i, r in board.head(top).iterrows():
        pf = "inf" if np.isinf(r["profit_factor"]) else f"{r['profit_factor']:.2f}"
        mark = "★" if r["ready"] else " "
        lines.append(
            f"{i + 1:>2} {mark}{str(r.get('strategy', ''))[:18]:<18}{str(r.get('asset_class', ''))[:6]:<7}"
            f"{r['trades']:>4}{r['win_rate'] * 100:>6.1f}{pf:>6}{r['expectancy_r']:>7.2f}"
            f"{r['net_pnl']:>10,.0f}{r['max_drawdown_pct']:>6.1f}"
        )
    lines.append("</pre>")
    ready = int(board["ready"].sum())
    lines.append(f"★ = meets promotion criteria ({ready} of {len(board)}). Live trading stays locked until you sign off.")
    return "\n".join(lines)


def account_line(trades: pd.DataFrame, account: str, starting_balance: float) -> str:
    s = stats(trades[trades["account"] == account], starting_balance)
    return (f"Running {account}: {s['wins']}W/{s['losses']}L ({s['win_rate'] * 100:.0f}%) · "
            f"net ${s['net_pnl']:,.2f} · equity ${starting_balance + s['net_pnl']:,.2f}")
