"""
Rolling wave-analysis state for live mode.

Holds a bounded buffer of OHLCV bars, re-runs the pivot/wave/prediction/
alert pipeline whenever a new bar arrives, and exposes a JSON-serializable
snapshot for the dashboard to poll. Deliberately has no Flask or network
code in it, so it can be unit-tested with synthetic bars alone.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Optional

import pandas as pd

from .live_feed import Bar
from .wave_detector import find_zigzag_pivots, label_current_wave
from .predictor import predict
from .alert_system import check_invalidation, check_sudden_move
from .portfolio import PaperPortfolio


class LiveWaveState:
    def __init__(self, ticker: str, provider: str = "unknown", max_bars: int = 500,
                 zigzag_threshold_pct: float = 4.0,
                 wave4_overlap_tolerance_pct: float = 0.0,
                 min_wave3_extension: float = 1.0,
                 invalidation_buffer_pct: float = 0.5,
                 sudden_move_atr_multiple: float = 3.0,
                 initial_df: Optional[pd.DataFrame] = None,
                 paper_starting_cash: float = 100_000.0,
                 maintenance_margin_ratio: float = 0.25,
                 buffer_start_ratio: float = 0.45,
                 leverage_tiers: Optional[list[tuple[float, float]]] = None):
        self.ticker = ticker
        self.provider = provider
        self.max_bars = max_bars
        self.zigzag_threshold_pct = zigzag_threshold_pct
        self.wave4_overlap_tolerance_pct = wave4_overlap_tolerance_pct
        self.min_wave3_extension = min_wave3_extension
        self.invalidation_buffer_pct = invalidation_buffer_pct
        self.sudden_move_atr_multiple = sudden_move_atr_multiple

        self.portfolio = PaperPortfolio(
            ticker=ticker, starting_cash=paper_starting_cash,
            maintenance_margin_ratio=maintenance_margin_ratio,
            buffer_start_ratio=buffer_start_ratio,
            tiers=leverage_tiers or [(0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50)],
        )

        self._lock = threading.Lock()
        self._df = initial_df.copy() if initial_df is not None else pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        )
        self._snapshot: dict = {"ticker": ticker, "provider": provider, "status": "warming_up"}
        if len(self._df) >= 5:
            self._recompute()

    def add_bar(self, bar: Bar) -> None:
        with self._lock:
            ts = pd.Timestamp(bar.timestamp)
            row = {"open": bar.open, "high": bar.high, "low": bar.low,
                   "close": bar.close, "volume": bar.volume}
            if ts in self._df.index:
                self._df.loc[ts] = row  # update in-progress bar
            else:
                self._df.loc[ts] = row
                self._df = self._df.sort_index()
            if len(self._df) > self.max_bars:
                self._df = self._df.iloc[-self.max_bars:]
            self._recompute()

    def _recompute(self) -> None:
        """Must be called with self._lock held."""
        df = self._df
        snapshot = {
            "ticker": self.ticker,
            "provider": self.provider,
            "last_update": str(df.index[-1]) if len(df) else None,
            "last_price": float(df["close"].iloc[-1]) if len(df) else None,
            "bars": [
                {"t": str(ts), "o": float(row.open), "h": float(row.high),
                 "l": float(row.low), "c": float(row.close), "v": float(row.volume)}
                for ts, row in df.iterrows()
            ],
        }

        last_price = float(df["close"].iloc[-1]) if len(df) else None
        if last_price is not None:
            self.portfolio.mark_to_market(last_price)
        snapshot["portfolio"] = self.portfolio.to_dict(last_price)

        pivots = find_zigzag_pivots(df, self.zigzag_threshold_pct)
        wave_count = label_current_wave(
            pivots, self.wave4_overlap_tolerance_pct, self.min_wave3_extension
        )

        if wave_count is None:
            snapshot["status"] = "warming_up"
            snapshot["wave_count"] = None
            snapshot["prediction"] = None
            snapshot["alerts"] = []
            self._snapshot = snapshot
            return

        snapshot["status"] = "ready"
        snapshot["wave_count"] = {
            "wave_type": wave_count.wave_type,
            "current_wave": wave_count.current_wave,
            "confidence": wave_count.confidence,
            "is_valid": wave_count.is_valid,
            "violations": wave_count.violations,
            "pivots": [{"t": str(p.date), "price": p.price, "kind": p.kind}
                       for p in wave_count.pivots],
        }

        prediction = predict(wave_count)
        snapshot["prediction"] = {
            "invalidation_level": prediction.invalidation_level,
            "invalidation_note": prediction.invalidation_note,
            "targets": [asdict(t) for t in prediction.targets],
        }

        last_price = float(df["close"].iloc[-1])
        alerts = check_invalidation(prediction, last_price, self.invalidation_buffer_pct)
        alerts += check_sudden_move(df, atr_multiple=self.sudden_move_atr_multiple)
        snapshot["alerts"] = [{"level": a.level, "message": a.message} for a in alerts]

        self._snapshot = snapshot

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def place_order(self, side: str, notional_usd: float, leverage: float):
        """Open a paper position at the current mark price. Thread-safe;
        refreshes the snapshot immediately so the UI doesn't wait for the
        next bar to reflect the new position."""
        with self._lock:
            last_price = float(self._df["close"].iloc[-1]) if len(self._df) else None
            if last_price is None:
                from .portfolio import OrderResult
                return OrderResult(False, "No price data yet — wait for the first bar.")
            result = self.portfolio.open(side, notional_usd, leverage, last_price)
            self._recompute()
            return result

    def close_position(self):
        """Close the current paper position at the current mark price."""
        with self._lock:
            last_price = float(self._df["close"].iloc[-1]) if len(self._df) else None
            if last_price is None:
                from .portfolio import OrderResult
                return OrderResult(False, "No price data yet.")
            result = self.portfolio.close(last_price)
            self._recompute()
            return result
