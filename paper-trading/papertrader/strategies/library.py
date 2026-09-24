"""Strategy library. Every strategy works on any asset class (crypto, stocks, FX)."""
import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import Strategy


def _side(long_cond, short_cond):
    return np.where(long_cond, 1, np.where(short_cond, -1, 0))


class EmaCrossover(Strategy):
    name = "ema_crossover"
    description = "Fast/slow EMA cross in the direction of the long-term trend (WEMA-style trend follower)."
    default_params = {"fast": 9, "slow": 21, "trend": 200}

    def rules(self, df):
        c = df["close"]
        fast, slow, trend = ind.ema(c, self.params["fast"]), ind.ema(c, self.params["slow"]), ind.ema(c, self.params["trend"])
        up, down = ind.crossed_above(fast, slow), ind.crossed_below(fast, slow)
        entry = _side(up & (c > trend), down & (c < trend))
        return entry, down, up


class RsiMeanReversion(Strategy):
    name = "rsi_reversion"
    description = "Buy oversold dips in an uptrend / sell overbought rips in a downtrend; exit when RSI returns to 50."
    default_params = {"period": 14, "oversold": 30, "overbought": 70, "trend": 200, "stop_atr": 1.5, "target_atr": 2.0}

    def rules(self, df):
        c = df["close"]
        r = ind.rsi(c, self.params["period"])
        trend = ind.sma(c, self.params["trend"])
        long_ = ind.crossed_above(r, self.params["oversold"]) & (c > trend)
        short = ind.crossed_below(r, self.params["overbought"]) & (c < trend)
        return _side(long_, short), r >= 50, r <= 50


class BollingerBounce(Strategy):
    name = "bollinger_bounce"
    description = "Close re-enters the Bollinger Band after piercing it (port of BB-Top-Bounce); exit at the middle band."
    default_params = {"period": 20, "std": 2.0, "stop_atr": 1.5, "target_atr": 2.5}

    def rules(self, df):
        c = df["close"]
        mid, upper, lower = ind.bollinger(c, self.params["period"], self.params["std"])
        long_ = ind.crossed_above(c, lower)
        short = ind.crossed_below(c, upper)
        return _side(long_, short), c >= mid, c <= mid


class KeltnerBollingerSqueeze(Strategy):
    name = "keltner_squeeze"
    description = "Bollinger Bands contract inside Keltner Channels, then trade the breakout (port of Keltner_Bollinger)."
    default_params = {"period": 20, "bb_std": 2.0, "kc_mult": 1.5, "min_squeeze_bars": 5}

    def rules(self, df):
        c = df["close"]
        bb_mid, bb_up, bb_lo = ind.bollinger(c, self.params["period"], self.params["bb_std"])
        _, kc_up, kc_lo = ind.keltner(df, self.params["period"], self.params["kc_mult"])
        squeeze = (bb_up < kc_up) & (bb_lo > kc_lo)
        n = self.params["min_squeeze_bars"]
        was_squeezed = squeeze.shift(1).rolling(n, min_periods=n).sum() == n
        released = was_squeezed & ~squeeze
        long_ = released & (c > bb_mid)
        short = released & (c < bb_mid)
        return _side(long_, short), ind.crossed_below(c, bb_mid), ind.crossed_above(c, bb_mid)


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    description = "Turtle-style: close breaks the N-bar high/low; exit on the opposite M-bar channel."
    default_params = {"entry_period": 20, "exit_period": 10, "stop_atr": 2.0, "target_atr": 4.0}

    def rules(self, df):
        c = df["close"]
        up, lo = ind.donchian(df, self.params["entry_period"])
        ex_up, ex_lo = ind.donchian(df, self.params["exit_period"])
        return _side(c > up, c < lo), c < ex_lo, c > ex_up


class MacdTrend(Strategy):
    name = "macd_trend"
    description = "MACD crosses its signal line with price on the right side of the 200 EMA."
    default_params = {"fast": 12, "slow": 26, "signal": 9, "trend": 200}

    def rules(self, df):
        c = df["close"]
        line, sig, _ = ind.macd(c, self.params["fast"], self.params["slow"], self.params["signal"])
        trend = ind.ema(c, self.params["trend"])
        up, down = ind.crossed_above(line, sig), ind.crossed_below(line, sig)
        return _side(up & (c > trend), down & (c < trend)), down, up
