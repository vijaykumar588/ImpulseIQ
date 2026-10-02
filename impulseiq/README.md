# IMPULSEIQ

## Quick start (no manual commands)

**Windows:** double-click `ImpulseIQ.bat`
**macOS/Linux:** double-click `ImpulseIQ.command` (first time only, right-click it → "Open", since it's unsigned; or run `chmod +x ImpulseIQ.command` once in a terminal)

That's it. It installs anything missing on first run, asks which ticker and data source you want, starts the dashboard, and opens your browser to it automatically. Python 3.11+ must already be installed on the system (get it free from [python.org](https://python.org/downloads) if not) — everything else is handled for you.

Prefer the command line, or want to script/automate it? See "Manual setup" below.

Elliott Wave analysis engine for US equities. Detects pivot structure, labels
Elliott Wave counts against the classical rulebook, projects likely
continuation targets with a confidence score, raises early warnings when
price action invalidates the current wave count, and includes a **dynamic
buffer liquidation** module for leveraged positions (staged partial closes
instead of a single full liquidation at the margin-call line).

> ⚠️ **Not financial advice.** Elliott Wave counting is a heuristic, subjective
> methodology. Two analysts can label the same chart differently. This tool
> enforces the *hard* Elliott Wave rules (which are objective) and scores the
> *guideline* rules (which are probabilistic) — it does not guarantee future
> price behavior. Use it as one input among many, and paper-trade any
> strategy before risking capital.

## Why Yahoo Finance instead of TradingView

TradingView does not publish a public data API — its charts are for
TradingView's own UI, and scraping them violates its Terms of Service. So the
data layer here (`src/data_fetcher.py`) is built behind a small interface:

```python
class DataSource(ABC):
    def get_ohlcv(self, ticker, interval, lookback) -> pd.DataFrame: ...
```

`YFinanceSource` (using the free `yfinance` package) is the default
implementation. If you have access to a broker/data API that *does* allow
programmatic pulls (Alpaca, Polygon.io, IBKR, or a TradingView paid
webhook/alert feed you own), drop in a new class implementing the same
interface and point `config.yaml` at it — nothing else in the codebase
changes.

## Architecture

```
ImpulseIQ.bat            # Windows one-click launcher (installs deps, runs, opens browser)
ImpulseIQ.command        # macOS/Linux equivalent
launch.py                 # Cross-platform launcher logic both of the above call into
src/
  data_fetcher.py      # OHLCV retrieval (pluggable data source)
  wave_rules.py         # Objective Elliott Wave rules + Fibonacci constants
  wave_detector.py      # Zigzag pivot detection + wave labeling engine
  predictor.py           # Fibonacci-based target projection + confidence score
  alert_system.py        # Invalidation / sudden-reversal early warnings
  leverage_manager.py    # Dynamic buffer staged liquidation
  portfolio.py             # Paper trading portfolio, wired to leverage_manager's engine
  live_feed.py            # Streaming/polling live bar sources (simulated, yfinance, Alpaca)
  live_state.py           # Rolling buffer + re-run of wave/alert/portfolio pipeline per live bar
  dashboard.py             # Flask app: live chart, JSON state endpoint, buy/sell/close endpoints
  templates/index.html     # Live dashboard UI (lightweight-charts, portfolio panel, risk gauge, audit log)
  main.py                 # CLI entry point tying it all together
tests/
  test_wave_detector.py  # Rule-validation unit tests (synthetic data, no network)
  test_live.py             # Live-feed and rolling-state unit tests (synthetic data, no network)
  test_portfolio.py        # Paper trading + liquidation-engine wiring tests (synthetic data, no network)
```

## Install (manual setup)

```bash
pip install -r requirements.txt
```

## Usage

```bash
python -m src.main --ticker AAPL --interval 1d --lookback 250
```

Example output:

```
Ticker: AAPL  Interval: 1d
Detected pivots: 11
Current wave count: Impulse, Wave (3) of 5  [confidence: 0.71]
  Wave 1: 2024-11-04 -> 2024-12-02   +8.2%
  Wave 2: 2024-12-02 -> 2024-12-18   -3.1% (retrace 0.42 of Wave 1)
  Wave 3: 2024-12-18 -> in progress   +14.7% so far
Invalidation level: 172.14 (close below this breaks the Wave-2 low; count fails)
Next target zone (Wave 3 = 1.618x Wave 1): 198.40 - 204.10
Alert: none — price action consistent with active count.
```

Run the leverage-manager demo:

```bash
python -m src.main --demo-leverage
```

## Running tests

```bash
pytest tests/ -v
```

No network access is required for tests — they run against synthetic OHLCV
series and a synthetic live feed built to satisfy (or deliberately violate)
each Elliott Wave rule.

## Live dashboard (real-time graphs + paper trading)

```bash
python -m src.main --live --ticker AAPL --provider simulated
```

Then open `http://127.0.0.1:8000`. You get:

- A live-updating candlestick chart (via TradingView's own open-source
  `lightweight-charts` library), with the ticker, live price, and a
  color-coded data-source badge (live / delayed / simulated) shown directly
  on the chart — never ambiguous about what you're looking at
- The current Elliott Wave count, confidence, and rule-validity, with pivot
  markers on the chart
- Fibonacci target zones and the wave-invalidation price line
- **Paper trading**: BUY/LONG or SELL/SHORT with a chosen notional size and
  leverage, filled at the live mark price. No real money or real orders —
  purely a local simulation
- **Live dynamic-buffer de-leveraging**: once you have an open position, the
  same staged-liquidation logic from `leverage_manager.py` runs against it
  on every new bar — not a separate demo, the actual engine acting on your
  actual (paper) position. As the price moves against you, it partially
  closes the position in tiers instead of one full liquidation, and every
  action is written to the on-screen audit log
- A **liquidation price line** and **buffer-start line** drawn on the chart
  the moment you open a position, plus small stage labels ("Stage 1: sell
  10%…") at each tier's trigger price — so you can see exactly where the
  de-leveraging will fire *before* it happens
- A **system risk gauge** mapping your position's equity ratio to
  Low/Moderate/High/Red/Extreme — a visual aid, not a rigorous risk model

`--provider` controls where the live bars come from:

| Provider | Setup | How "real-time" it actually is |
|---|---|---|
| `simulated` (default) | none | Synthetic random-walk bars — zero setup, good for trying the UI immediately or running it in a sandbox with no network |
| `yfinance` | none | Polls Yahoo Finance on an interval (`--poll-seconds`, default 30). **Not** true real-time — Yahoo's intraday data is typically delayed ~15-20 minutes |
| `alpaca` | free API key | Genuinely real-time — streams live bars over Alpaca's free-plan IEX websocket the moment they print. Copy `.env.example` to `.env` and fill in `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` from a free account at alpaca.markets |

The `alpaca` path is the one that gives you actual real-time graphs; the
other two exist so the rest of the pipeline (chart, wave detection, paper
trading, alerts) is usable and testable with zero external dependencies.

Paper trading rules, deliberately simple for reliability in a live demo:
one open position at a time (long OR short); you must close it before
opening another; market orders only, filled at the current mark price;
starting cash is configurable via `paper_trading.starting_cash` in
`config.yaml` (default $100,000).

The dashboard backfills recent history first (via Yahoo Finance) so the
chart isn't empty on load, then switches to whichever live provider you
chose for new bars. The server is a small local Flask app meant to be run
on your own machine — it has no auth and isn't meant to be exposed to the
internet as-is.



Traditional margin systems liquidate a leveraged position **entirely** the
moment equity crosses the maintenance-margin line. That single large forced
sell:

- dumps the full position size into the market at once (worse fill/slippage)
- can leave the platform under-collateralized if price gaps through the
  liquidation price before the order fills (the "bad debt" problem)

`leverage_manager.py` instead defines liquidation **tiers** between a buffer
threshold and the hard maintenance line. As equity ratio degrades through
each tier, a **percentage** of the position is closed — reducing leverage
(and risk) gradually, so:

- the position size shrinks *before* the hard line is reached, cutting
  gap/slippage exposure at the final tier
- most of the position, if price recovers, survives instead of being wiped

This is a portfolio-risk simulation module — it operates on positions you
feed it (ticker, size, entry, leverage, mark price) and returns liquidation
actions. It does not place real orders; wire its output into your broker API
if you want it to act live, and paper-trade it first.

## Disclaimer

This project is for research and educational purposes. It does not
constitute investment advice. Markets are not fully predictable by any
technical method, Elliott Wave included. Past pattern completion rates do
not guarantee future results. You are responsible for any trading decisions
and any capital you risk.
