from pathlib import Path

import yaml

from .broker import Costs, PaperBroker
from .engine import Instrument
from .safety import assert_paper_mode
from .store import Store
from .strategies import Strategy, build


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{path} not found - copy config.example.yaml to config.yaml first")
    cfg = yaml.safe_load(p.read_text()) or {}
    assert_paper_mode(cfg)
    return cfg


def make_broker(cfg: dict, db_path: str | None = None) -> PaperBroker:
    risk = cfg.get("risk", {})
    costs = {k: Costs(**v) for k, v in cfg.get("costs", {}).items()}
    return PaperBroker(
        store=Store(db_path or cfg.get("database", "data/paper.db")),
        starting_balance=float(cfg.get("starting_balance", 10_000)),
        risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 1.0)),
        costs=costs,
        max_leverage={k: float(v) for k, v in risk.get("max_leverage", {}).items()},
    )


def instruments(cfg: dict, market: str | None = None) -> list[tuple[Instrument, str]]:
    out = []
    for asset_class, m in cfg.get("markets", {}).items():
        if market and asset_class != market:
            continue
        for sym in m.get("symbols", []):
            inst = Instrument(sym, asset_class, m.get("timeframe", "1h"), bool(m.get("allow_short", True)))
            out.append((inst, m.get("source", "yfinance")))
    return out


def strategies(cfg: dict, only: str | None = None) -> list[tuple[Strategy, list[str] | None]]:
    out = []
    for label, s in cfg.get("strategies", {}).items():
        s = s or {}
        if only and label != only:
            continue
        if not s.get("enabled", True) and not only:
            continue
        # `type` lets one strategy run several times with different settings, e.g.
        #   ema_fast: {type: ema_crossover, params: {fast: 5, slow: 13}}
        out.append((build(s.get("type", label), s.get("params"), label=label), s.get("markets")))
    return out
