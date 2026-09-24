"""SQLite journal of paper positions, closed trades and scan state."""
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT, strategy TEXT, symbol TEXT, asset_class TEXT, timeframe TEXT,
    side INTEGER, qty REAL, entry_price REAL, entry_time TEXT,
    stop REAL, target REAL, risk_amount REAL, entry_fee REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT, strategy TEXT, symbol TEXT, asset_class TEXT, timeframe TEXT,
    side INTEGER, qty REAL, entry_price REAL, entry_time TEXT,
    exit_price REAL, exit_time TEXT, exit_reason TEXT,
    pnl REAL, fees REAL, r_multiple REAL, return_pct REAL
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Position:
    account: str
    strategy: str
    symbol: str
    asset_class: str
    timeframe: str
    side: int
    qty: float
    entry_price: float
    entry_time: str
    stop: float
    target: float
    risk_amount: float
    entry_fee: float
    id: int | None = None


@dataclass
class Trade:
    account: str
    strategy: str
    symbol: str
    asset_class: str
    timeframe: str
    side: int
    qty: float
    entry_price: float
    entry_time: str
    exit_price: float
    exit_time: str
    exit_reason: str
    pnl: float
    fees: float
    r_multiple: float
    return_pct: float
    id: int | None = None


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- positions ---------------------------------------------------------
    def add_position(self, p: Position) -> Position:
        data = {k: v for k, v in asdict(p).items() if k != "id"}
        cur = self.db.execute(
            f"INSERT INTO positions ({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values())
        )
        self.db.commit()
        p.id = cur.lastrowid
        return p

    def open_position(self, strategy: str, symbol: str, timeframe: str) -> Position | None:
        row = self.db.execute(
            "SELECT * FROM positions WHERE strategy=? AND symbol=? AND timeframe=?", (strategy, symbol, timeframe)
        ).fetchone()
        return Position(**dict(row)) if row else None

    def positions(self) -> list[Position]:
        return [Position(**dict(r)) for r in self.db.execute("SELECT * FROM positions ORDER BY entry_time")]

    def remove_position(self, position_id: int):
        self.db.execute("DELETE FROM positions WHERE id=?", (position_id,))
        self.db.commit()

    # -- trades ------------------------------------------------------------
    def add_trade(self, t: Trade) -> Trade:
        data = {k: v for k, v in asdict(t).items() if k != "id"}
        cur = self.db.execute(
            f"INSERT INTO trades ({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values())
        )
        self.db.commit()
        t.id = cur.lastrowid
        return t

    def trades(self) -> pd.DataFrame:
        cols = [f.name for f in fields(Trade)]
        return pd.read_sql_query("SELECT * FROM trades ORDER BY exit_time, id", self.db).reindex(columns=cols)

    def realized_pnl(self, account: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE account=?", (account,)).fetchone()
        return float(row[0])

    # -- state -------------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()
