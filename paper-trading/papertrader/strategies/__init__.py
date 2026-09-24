from .base import Strategy
from .library import (
    BollingerBounce,
    DonchianBreakout,
    EmaCrossover,
    KeltnerBollingerSqueeze,
    MacdTrend,
    RsiMeanReversion,
)

REGISTRY = {
    cls.name: cls
    for cls in (EmaCrossover, RsiMeanReversion, BollingerBounce, KeltnerBollingerSqueeze, DonchianBreakout, MacdTrend)
}


def build(name: str, params: dict | None = None) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"Unknown strategy '{name}'. Available: {', '.join(REGISTRY)}")
    return REGISTRY[name](**(params or {}))


__all__ = ["Strategy", "REGISTRY", "build"]
