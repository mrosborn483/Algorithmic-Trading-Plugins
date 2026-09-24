from typing import ClassVar

import pandas as pd

from .. import indicators as ind


class Strategy:
    """Base class for all strategies.

    Subclasses implement `rules(df)` and return three boolean/int series:
      entry      +1 = go long, -1 = go short, 0 = nothing (evaluated on bar close)
      exit_long  True = close an open long on this bar's close
      exit_short True = close an open short on this bar's close

    Stops and targets are ATR based and shared by every strategy so results are comparable.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    default_params: ClassVar[dict] = {}
    base_params: ClassVar[dict] = {"atr_period": 14, "stop_atr": 2.0, "target_atr": 3.0}

    # Small grid of alternative settings tried by `sweep` (stops/targets are swept for every strategy)
    grid: ClassVar[dict] = {}
    stop_target_grid: ClassVar[list] = [(1.5, 2.0), (2.0, 3.0), (2.0, 4.0)]

    def __init__(self, label: str | None = None, **params):
        # label identifies this instance, so one strategy type can run with several settings
        self.label = label or self.name
        unknown = set(params) - set(self.default_params) - set(self.base_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params = {**self.base_params, **self.default_params, **params}

    def rules(self, df: pd.DataFrame):
        raise NotImplementedError

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        entry, exit_long, exit_short = self.rules(df)
        out["entry"] = pd.Series(entry, index=df.index).fillna(0).astype(int)
        out["exit_long"] = pd.Series(exit_long, index=df.index).fillna(False).astype(bool)
        out["exit_short"] = pd.Series(exit_short, index=df.index).fillna(False).astype(bool)
        out["atr"] = ind.atr(df, self.params["atr_period"])
        return out

    def stops(self, side: int, price: float, atr_value: float):
        stop = price - side * self.params["stop_atr"] * atr_value
        target = price + side * self.params["target_atr"] * atr_value
        return stop, target

    def __repr__(self):
        return f"{self.label}<{self.name}>({self.params})"
