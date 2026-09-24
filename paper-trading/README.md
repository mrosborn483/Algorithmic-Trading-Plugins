# Paper Trader: crypto · stocks · FX

A paper-trading engine that runs several strategies across crypto, stocks and FX. It sends
every simulated entry and every **WIN / LOSS** to Telegram and ranks the strategies, so you
can pick which ones deserve real money.

> **Live trading is locked.** There is no live broker in this code, and any config with
> `mode` set to anything but `paper` is refused. Automation (schedulers, always-on loops,
> live order routing) is also held back on purpose. You run `scan` yourself until the
> paper phase is finalized.

## Quick start

```bash
cd paper-trading
pip install -r requirements.txt
cp config.example.yaml config.yaml         # pick symbols, timeframes, risk, strategies

python -m papertrader strategies           # list strategies
python -m papertrader backtest             # rank every strategy x market on history
python -m papertrader scan --dry-run       # one paper pass, alerts printed instead of sent
python -m papertrader scan                 # one paper pass, alerts sent to Telegram
python -m papertrader positions            # open paper positions
python -m papertrader report --send        # wins/losses leaderboard -> Telegram
```

Market data comes from Yahoo Finance by default and needs no API keys: `AAPL`, `EURUSD=X`,
`BTC-USD`. For crypto you can use an exchange feed instead (`pip install ccxt`, then set
`source: ccxt:kraken` with symbols such as `BTC/USD`).

## Telegram setup

1. In Telegram, message **@BotFather**, send `/newbot` and copy the bot token.
2. Send any message to your new bot (or add it to a group).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
4. Export the credentials. Never put them in the config or commit them:
   ```bash
   export TELEGRAM_BOT_TOKEN=123456:ABC...
   export TELEGRAM_CHAT_ID=987654321
   python -m papertrader telegram-test
   ```

What you'll receive:

| Alert | When |
|---|---|
| 🧪 PAPER 🟢 LONG / 🔴 SHORT | a strategy opens a paper trade (entry, stop, target, size, $ risk) |
| 🧪 ✅ WIN / ❌ LOSS | a trade closes: P&L in $, R-multiple, % and exit reason, plus that strategy's running W/L record |
| 📊 Leaderboard | `report --send` or `backtest --send` |
| ⚠️ Data unavailable | a market feed failed during a scan |

## Strategies

| Name | Idea | Default exit |
|---|---|---|
| `ema_crossover` | 9/21 EMA cross in the direction of the 200 EMA (WEMA-style) | opposite cross, ATR stop/target |
| `rsi_reversion` | RSI leaves oversold in an uptrend / overbought in a downtrend | RSI back to 50 |
| `bollinger_bounce` | close re-enters the Bollinger Band (port of `BB-Top-Bounce`) | middle band |
| `keltner_squeeze` | BB inside Keltner for 5+ bars, then trade the release (port of `Keltner_Bollinger`) | close crosses middle band |
| `donchian_breakout` | 20-bar high/low breakout (Turtle) | 10-bar opposite channel |
| `macd_trend` | MACD/signal cross on the right side of the 200 EMA | opposite cross |

Every strategy also uses an ATR stop and target (`stop_atr`, `target_atr`). To add one,
subclass `Strategy` in `papertrader/strategies/library.py`, return
`entry, exit_long, exit_short` from `rules()`, and register it in `strategies/__init__.py`.

## How the paper simulation works

- **Signals only on closed bars.** The bar still forming is dropped, so signals never repaint.
- **One virtual account per strategy × asset class**, each starting at `starting_balance`.
  That lets you compare `ema_crossover` on crypto directly with `ema_crossover` on FX.
- **Risk-based sizing.** Each trade risks `risk_per_trade_pct` of that account's equity,
  measured to the stop, with position size capped by `max_leverage`.
- **Costs.** Fees and slippage are charged per side for each asset class.
- **Stops and targets** are checked against each bar's high and low. A gap fills at the open.
  If both levels fall inside the same bar, the stop is assumed to have hit first
  (the conservative choice).
- **Scans can be run irregularly.** Each `scan` checks every bar since the last one for
  stop, target or exit hits, so nothing is missed. New entries are only taken on the latest
  bar, so you never get a stale fill. The first scan does not replay history as fake trades.
- The backtester uses the exact same code path, so backtest and paper results are comparable.
- Everything is logged to SQLite (`data/paper.db`). `report --csv board.csv` exports the
  leaderboard and every trade.

## Choosing strategies for live trading

`report` ranks each strategy × market and marks with ★ the ones that meet all of the
`promotion` criteria in the config:

- at least 30 closed trades
- profit factor ≥ 1.3
- win rate ≥ 35%
- expectancy ≥ 0.10R
- max drawdown ≤ 15%

Suggested process:

1. Run `backtest` and switch off or retune strategy × market pairs that are clearly negative.
2. Paper-trade the rest for several weeks, until each has 30 or more trades, and run `scan` regularly.
3. Keep only the ★ pairs whose paper results roughly match their backtest.
4. Only then build the live broker adapter and the automation (scheduler, kill switch,
   daily loss limit). That work is intentionally not started.

## Tests

```bash
python -m pytest -q
```
