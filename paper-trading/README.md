# Paper Trader: crypto · stocks · FX

A paper-trading engine that runs several strategies across crypto, stocks and FX. It sends
every simulated entry and every **WIN / LOSS** to Telegram and ranks the strategies, so you
can pick which ones deserve real money.

> **Live trading is locked.** There is no live broker in this code, and any config with
> `mode` set to anything but `paper` is refused. The only automation is the **paper-only**
> hourly scheduler (`run`). It re-checks paper mode before every scan and stops if the
> config asks for anything else.

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
python -m papertrader run                  # PAPER-ONLY scheduler: scan hourly + daily report
python -m papertrader sweep --write best.yaml   # best strategy/settings per market over the past week
python -m papertrader backtest --days 7    # your configured strategies over the past week
python -m papertrader report --ai          # leaderboard + did the AI's calls hold up?
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

### Running more strategies

There's no fixed limit. Every strategy runs against the same downloaded bars, so adding
strategies costs almost nothing: hundreds of strategy × market combinations scan in seconds.
To run one strategy several times with different settings, give each copy its own name and
a `type` in `config.yaml`:

```yaml
strategies:
  ema_fast_crypto:
    type: ema_crossover
    markets: [crypto]
    params: {fast: 5, slow: 21, stop_atr: 1.5, target_atr: 2.0}
```

The real limit is statistical, not technical. The more variants you run, the more of them
will look good purely by luck. That's why the promotion criteria require 30 or more trades,
and why `sweep` checks every recent winner against a longer window.

## Finding the best strategy right now (`sweep`)

```bash
python -m papertrader sweep                      # last 7 days, checked against 90 days, all markets
python -m papertrader sweep --days 14 --market crypto --top 10
python -m papertrader sweep --write best.yaml --send
```

`sweep` tries every strategy with a grid of settings (about 160 variants, including different
stop and target distances) on every market, in parallel across all CPU cores. It ranks them
on the recent window. It then shows each variant's result over the longer window next to it
and marks ✓ the ones that also hold up there (expectancy ≥ 0.10R, profit factor ≥ 1.2,
10 or more trades). A variant that shines this week but lost money over 90 days was probably lucky.

`--write best.yaml` saves the top ✓ variants per market as a ready-made `strategies:` block.
Paste it into `config.yaml` so the scheduler starts paper-trading them. Re-running the sweep
weekly and rotating strategies works, but always judge the result by paper results, not the sweep.

`backtest --days 7` answers a narrower question: how the strategies already in your config
did over the past N days.

## AI trade reviewer (Claude)

Setting `ai.enabled: true` in `config.yaml` (and `ANTHROPIC_API_KEY` in `.env`) makes Claude
review every new signal before the paper trade opens. Claude receives the last 30 bars, key
indicators, the daily trend, and that strategy's own paper track record, and returns
**take / skip**, a confidence and a one-line reason.

It's set up as an experiment you can measure, not a black box you have to trust:

- **Twin accounts.** Each strategy keeps trading every signal. A twin account
  `<strategy>+ai` trades only the signals Claude approves. In the leaderboard,
  `ema_crossover` sits next to `ema_crossover+ai`, so you can see directly whether the AI helps.
- **Every alert shows the verdict**, e.g. `🤖 AI: SKIP (35%) - entry is extended into
  resistance, daily trend down`.
- **`report --ai`** shows the win rate and average R of signals Claude approved versus rejected.
  If rejected signals do just as well, the AI isn't adding anything. Turn it off.
- **Safe failure.** If the API is down, over budget, or declines to answer, the twin skips
  that trade and the plain strategy carries on unaffected.
- **Cost cap.** `max_calls_per_day` limits spending; a review costs a few cents. Use
  `ai.strategies` to review only some strategies.
- **Forward paper trades only, never backtests.** The model may already know how past
  prices moved, so backtested AI results would look better than they really are.

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

## Hourly scheduler (paper only)

`python -m papertrader run` scans right away and then at 2 minutes past every hour, once the
hourly candles have closed. Once a day (`daily_report_utc`, 21:00 UTC by default) it also
sends the leaderboard to Telegram. Settings live in the `schedule:` block of `config.yaml`,
which is re-read before every scan, so edits apply without a restart.

- It announces itself in Telegram when it starts and stops.
- If a data feed or scan fails, it alerts you on the 1st, 3rd and every 24th consecutive
  failure (not every hour) and keeps going.
- A file lock stops a manual `scan` and a scheduled scan from running at the same time.
- If `mode` is changed away from `paper`, it sends ⛔ and exits.
- Use `run --dry-run` to try it without sending anything to Telegram.

## Deploying to a server

Any always-on Linux box works. The load is tiny: 1 vCPU, 1 GB RAM and well under 1 GB of disk.
It needs outbound HTTPS to Yahoo Finance (`query1/query2.finance.yahoo.com`) and
`api.telegram.org`. Keep the server clock in sync (NTP), because scans are aligned to UTC hours.

**Docker (recommended)**

```bash
git clone <this repo> && cd Algorithmic-Trading-Plugins/paper-trading
cp config.example.yaml config.yaml
cp .env.example .env                           # add TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
docker compose up -d --build                   # starts the paper-only scheduler, restarts on reboot
docker compose logs -f
docker compose run --rm papertrader report --send
docker compose run --rm papertrader backtest
```

The journal is kept in `./data/paper.db` on the host. Back that file up; it holds your paper track record.

**systemd (no Docker)**: see `deploy/papertrader.service`.

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
2. Paper-trade the rest with the scheduler for several weeks, until each has 30 or more trades.
3. Keep only the ★ pairs whose paper results roughly match their backtest.
4. Only then build the live broker adapter with a kill switch and a daily loss limit.
   That work is intentionally not started.

## Tests

```bash
python -m pytest -q
```
