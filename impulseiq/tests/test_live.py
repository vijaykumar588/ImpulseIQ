import time
import pandas as pd
import pytest

from src.live_feed import Bar, SimulatedLiveFeed, PollingFeed
from src.live_state import LiveWaveState


def test_simulated_feed_emits_bars():
    feed = SimulatedLiveFeed(start_price=100.0, interval_seconds=0.05, seed=42)
    received = []
    feed.start(received.append)
    time.sleep(0.3)
    feed.stop()

    assert len(received) >= 3
    assert all(isinstance(b, Bar) for b in received)
    assert all(b.high >= b.low >= 0 for b in received)


def test_simulated_feed_is_deterministic_with_seed():
    r1, r2 = [], []
    f1 = SimulatedLiveFeed(start_price=100.0, interval_seconds=0.02, seed=7)
    f1.start(r1.append)
    time.sleep(0.15)
    f1.stop()

    f2 = SimulatedLiveFeed(start_price=100.0, interval_seconds=0.02, seed=7)
    f2.start(r2.append)
    time.sleep(0.15)
    f2.stop()

    n = min(len(r1), len(r2), 3)
    assert n > 0
    for a, b in zip(r1[:n], r2[:n]):
        assert a.close == b.close


class _StubDataSource:
    """Feeds PollingFeed a growing DataFrame across successive calls."""
    def __init__(self, frames):
        self._frames = frames
        self._i = 0

    def get_ohlcv(self, ticker, interval, lookback):
        frame = self._frames[min(self._i, len(self._frames) - 1)]
        self._i += 1
        return frame


def _bar_df(rows):
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="min")
    return pd.DataFrame(rows, index=dates,
                         columns=["open", "high", "low", "close", "volume"])


def test_polling_feed_only_emits_new_bars():
    frame1 = _bar_df([[100, 101, 99, 100.5, 1000]])
    frame2 = _bar_df([[100, 101, 99, 100.5, 1000], [100.5, 102, 100, 101.5, 1200]])
    source = _StubDataSource([frame1, frame2, frame2])

    received = []
    feed = PollingFeed(source, "TEST", poll_seconds=0.05)
    feed.start(received.append)
    time.sleep(0.2)
    feed.stop()

    # First call emits 1 bar, second call emits only the 1 new bar, third
    # call (same frame again) emits nothing new.
    assert len(received) == 2
    assert received[0].close == 100.5
    assert received[1].close == 101.5


# --- LiveWaveState -------------------------------------------------------

def _make_bar(ts, o, h, l, c, v=1000):
    return Bar(timestamp=pd.Timestamp(ts).to_pydatetime(),
               open=o, high=h, low=l, close=c, volume=v)


def test_live_state_warms_up_then_becomes_ready():
    state = LiveWaveState("TEST", zigzag_threshold_pct=4.0)
    snap = state.snapshot()
    assert snap["status"] == "warming_up"

    bars = [
        _make_bar("2024-01-01", 100, 100, 100, 100),
        _make_bar("2024-01-02", 100, 110, 100, 110),
        _make_bar("2024-01-03", 110, 110, 105, 105),
        _make_bar("2024-01-04", 105, 125, 105, 125),
        _make_bar("2024-01-05", 125, 125, 118, 118),
        _make_bar("2024-01-06", 118, 135, 118, 135),
    ]
    for b in bars:
        state.add_bar(b)

    snap = state.snapshot()
    assert snap["status"] == "ready"
    assert snap["wave_count"] is not None
    assert snap["wave_count"]["is_valid"] is True
    assert snap["prediction"] is not None
    assert len(snap["bars"]) == len(bars)
    assert isinstance(snap["alerts"], list)


def test_live_state_updates_in_progress_bar_in_place():
    state = LiveWaveState("TEST")
    ts = "2024-01-01T00:00:00"
    state.add_bar(_make_bar(ts, 100, 101, 99, 100))
    state.add_bar(_make_bar(ts, 100, 103, 99, 102))  # same timestamp -> update, not append

    snap = state.snapshot()
    assert len(snap["bars"]) == 1
    assert snap["bars"][0]["c"] == 102


def test_live_state_respects_max_bars():
    state = LiveWaveState("TEST", max_bars=5)
    for i in range(10):
        state.add_bar(_make_bar(f"2024-01-{i+1:02d}", 100, 101, 99, 100 + i))
    snap = state.snapshot()
    assert len(snap["bars"]) == 5


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
