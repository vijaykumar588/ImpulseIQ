"""
Live/streaming bar feeds.

Three implementations, in order of "works with zero setup" to "genuinely
real-time":

  SimulatedLiveFeed  — synthetic random-walk bars. No network, no API key.
                        Good for trying the dashboard immediately and for
                        offline tests.

  PollingFeed         — wraps any existing DataSource (e.g. YFinanceSource)
                        and re-polls it on an interval, emitting only bars
                        newer than the last one seen. Works with no API key.
                        NOTE: Yahoo Finance intraday data is typically
                        delayed ~15-20 minutes, so this is "auto-refreshing",
                        not tick-real-time.

  AlpacaWebSocketFeed — true real-time (single-venue IEX data on Alpaca's
                        free plan) via their bars websocket channel. Needs a
                        free Alpaca API key (see .env.example). This talks to
                        a live external service and could not be exercised
                        against the real network in the environment this was
                        built in — the message protocol below matches
                        Alpaca's documented v2 market data stream as of this
                        writing, but test it against your own account before
                        relying on it, and check Alpaca's docs if anything
                        about the protocol has changed since.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


OnBarCallback = Callable[[Bar], None]


class LiveFeed(ABC):
    @abstractmethod
    def start(self, on_bar: OnBarCallback) -> None:
        """Start emitting bars asynchronously (non-blocking; runs its own thread)."""
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------

def generate_synthetic_backfill(start_price: float = 100.0, num_bars: int = 60,
                                 volatility_pct: float = 1.5,
                                 bar_seconds: float = 2.0,
                                 seed: Optional[int] = None) -> pd.DataFrame:
    """
    Instantly generates `num_bars` of synthetic OHLCV history using the same
    random-walk model as SimulatedLiveFeed, timestamped as if they'd just
    happened (ending at "now", spaced `bar_seconds` apart going backward).

    This exists purely so the simulated demo behaves like the real
    providers: yfinance/Alpaca both backfill recent history before the live
    feed starts, so the chart and wave count are populated immediately
    instead of the user waiting (with unpredictable, unseeded timing) for
    enough live bars to accumulate organically.
    """
    rng = random.Random(seed)
    price = start_price
    rows = []
    for _ in range(num_bars):
        move_pct = rng.uniform(-volatility_pct, volatility_pct) / 100
        open_p = price
        close_p = max(0.01, open_p * (1 + move_pct))
        high_p = max(open_p, close_p) * (1 + abs(rng.uniform(0, volatility_pct)) / 200)
        low_p = min(open_p, close_p) * (1 - abs(rng.uniform(0, volatility_pct)) / 200)
        price = close_p
        rows.append({
            "open": round(open_p, 4), "high": round(high_p, 4),
            "low": round(low_p, 4), "close": round(close_p, 4),
            "volume": rng.randint(1000, 50000),
        })

    now = datetime.now(timezone.utc)
    index = pd.date_range(end=now, periods=num_bars, freq=pd.Timedelta(seconds=bar_seconds))
    return pd.DataFrame(rows, index=index)


class SimulatedLiveFeed(LiveFeed):
    """
    Generates a synthetic random-walk bar every `interval_seconds`. Useful
    as a zero-setup default so the whole dashboard/wave-detection pipeline
    can be run and tested without any API key or network access.
    """

    def __init__(self, start_price: float = 100.0, interval_seconds: float = 2.0,
                 volatility_pct: float = 1.5, seed: Optional[int] = None):
        self.price = start_price
        self.interval_seconds = interval_seconds
        self.volatility_pct = volatility_pct
        self._rng = random.Random(seed)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self, on_bar: OnBarCallback) -> None:
        self._stop_event.clear()

        def _run():
            while not self._stop_event.is_set():
                move_pct = self._rng.uniform(-self.volatility_pct, self.volatility_pct) / 100
                open_p = self.price
                close_p = max(0.01, open_p * (1 + move_pct))
                high_p = max(open_p, close_p) * (1 + abs(self._rng.uniform(0, self.volatility_pct)) / 200)
                low_p = min(open_p, close_p) * (1 - abs(self._rng.uniform(0, self.volatility_pct)) / 200)
                self.price = close_p
                bar = Bar(
                    timestamp=datetime.now(timezone.utc),
                    open=round(open_p, 4), high=round(high_p, 4),
                    low=round(low_p, 4), close=round(close_p, 4),
                    volume=self._rng.randint(1000, 50000),
                )
                on_bar(bar)
                self._stop_event.wait(self.interval_seconds)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------

class PollingFeed(LiveFeed):
    """
    Wraps any `data_fetcher.DataSource` and polls it on an interval,
    emitting only bars strictly newer than the last one already seen.
    """

    def __init__(self, data_source, ticker: str, interval: str = "1m",
                 lookback: int = 5, poll_seconds: float = 30.0):
        self.data_source = data_source
        self.ticker = ticker
        self.interval = interval
        self.lookback = lookback
        self.poll_seconds = poll_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_ts: Optional[pd.Timestamp] = None

    def start(self, on_bar: OnBarCallback) -> None:
        self._stop_event.clear()

        def _run():
            while not self._stop_event.is_set():
                try:
                    df = self.data_source.get_ohlcv(self.ticker, self.interval, self.lookback)
                    for ts, row in df.iterrows():
                        if self._last_ts is None or ts > self._last_ts:
                            on_bar(Bar(
                                timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                                open=float(row["open"]), high=float(row["high"]),
                                low=float(row["low"]), close=float(row["close"]),
                                volume=float(row.get("volume", 0)),
                            ))
                            self._last_ts = ts
                except Exception as e:
                    print(f"[PollingFeed] fetch error: {e}")
                self._stop_event.wait(self.poll_seconds)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.poll_seconds + 5)


# --------------------------------------------------------------------------

class AlpacaWebSocketFeed(LiveFeed):
    """
    True real-time bars via Alpaca's market data websocket (free plan =
    single-venue IEX data). Requires `websockets` and a free API key
    (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, e.g. via .env).

    Protocol (per Alpaca's documented v2 stream, as of this writing):
      connect to wss://stream.data.alpaca.markets/v2/iex
      -> send {"action": "auth", "key": ..., "secret": ...}
      <- expect an auth-success message
      -> send {"action": "subscribe", "bars": [ticker]}
      <- receive one message per completed minute bar: {"T": "b", "S": ticker,
         "o", "h", "l", "c", "v", "t": ISO8601 timestamp, ...}

    This was written against Alpaca's documented protocol but could not be
    exercised against a live connection in the environment this was built
    in (no network access there). Test it against your own account; if the
    connection or message shape has changed, check
    https://docs.alpaca.markets/docs/streaming-market-data
    """

    STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"

    def __init__(self, ticker: str, api_key_id: str, api_secret_key: str):
        self.ticker = ticker
        self.api_key_id = api_key_id
        self.api_secret_key = api_secret_key
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self, on_bar: OnBarCallback) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_async_loop, args=(on_bar,), daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run_async_loop(self, on_bar: OnBarCallback) -> None:
        import asyncio
        asyncio.run(self._stream(on_bar))

    async def _stream(self, on_bar: OnBarCallback) -> None:
        import websockets

        backoff = 1
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.STREAM_URL) as ws:
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key": self.api_key_id,
                        "secret": self.api_secret_key,
                    }))
                    await ws.recv()  # auth ack — surface via logging if you need to debug

                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "bars": [self.ticker],
                    }))

                    backoff = 1  # reset on successful connect
                    while not self._stop_event.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.poll_check_interval())
                        for msg in json.loads(raw):
                            if msg.get("T") == "b" and msg.get("S") == self.ticker:
                                on_bar(Bar(
                                    timestamp=pd.to_datetime(msg["t"]).to_pydatetime(),
                                    open=float(msg["o"]), high=float(msg["h"]),
                                    low=float(msg["l"]), close=float(msg["c"]),
                                    volume=float(msg.get("v", 0)),
                                ))
            except asyncio.TimeoutError:
                continue  # just a poll-for-stop-event timeout, not a real error
            except Exception as e:
                print(f"[AlpacaWebSocketFeed] connection error: {e} "
                      f"— retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def poll_check_interval() -> float:
        return 5.0
