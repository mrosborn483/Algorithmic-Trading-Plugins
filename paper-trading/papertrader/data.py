"""Market data. Only *closed* bars are returned so signals never repaint.

Sources:
  yfinance          stocks ("AAPL"), FX ("EURUSD=X"), crypto ("BTC-USD")  - no API key
  ccxt:<exchange>   crypto from an exchange's public API, e.g. "ccxt:kraken" with "BTC/USD"
  csv:<directory>   <directory>/<symbol>.csv with timestamp,open,high,low,close,volume
"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

TF = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1D"}
# yfinance history limits: intraday <= 60d, hourly <= 730d
YF_PERIOD = {"5m": "59d", "15m": "59d", "30m": "59d", "1h": "729d", "4h": "729d", "1d": "10y"}


def tf_delta(timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(TF[timeframe])


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    idx = pd.to_datetime(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["open", "high", "low", "close"])


def drop_incomplete(df: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if not df.empty and df.index[-1] + tf_delta(timeframe) > now:
        return df.iloc[:-1]
    return df


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(TF[timeframe], label="left", closed="left").agg(agg).dropna(subset=["open"])


def _yfinance(symbol: str, timeframe: str, period: str | None) -> pd.DataFrame:
    import yfinance as yf

    interval = "1h" if timeframe == "4h" else timeframe
    raw = yf.Ticker(symbol).history(period=period or YF_PERIOD[timeframe], interval=interval, auto_adjust=True)
    df = _normalize(raw)
    return resample(df, "4h") if timeframe == "4h" else df


def _ccxt(exchange: str, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    import ccxt

    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return _normalize(df)


def _csv(directory: str, symbol: str, timeframe: str) -> pd.DataFrame:
    path = Path(directory) / f"{symbol.replace('/', '_')}.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = _normalize(df)
    return resample(df, timeframe) if len(df) > 1 and (df.index[1] - df.index[0]) < tf_delta(timeframe) else df


def fetch(symbol: str, timeframe: str, source: str = "yfinance", limit: int = 1000,
          period: str | None = None) -> pd.DataFrame:
    if timeframe not in TF:
        raise ValueError(f"timeframe must be one of {list(TF)}")
    if source == "yfinance":
        df = _yfinance(symbol, timeframe, period)
    elif source.startswith("ccxt:"):
        df = _ccxt(source.split(":", 1)[1], symbol, timeframe, limit)
    elif source.startswith("csv:"):
        df = _csv(source.split(":", 1)[1], symbol, timeframe)
    else:
        raise ValueError(f"unknown data source '{source}'")
    return drop_incomplete(df, timeframe)
