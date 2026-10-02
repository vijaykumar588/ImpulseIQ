"""
Early-warning alerts:
  - "approaching invalidation": price is within a buffer % of the level that
    would break the current wave count, flagged *before* it actually breaks.
  - "invalidated": the count has already been broken.
  - "sudden move": a bar's range is an outlier vs recent ATR, which often
    precedes a wave-count change and deserves a flag even if the count
    itself isn't broken yet.
"""

from dataclasses import dataclass
import pandas as pd

from .predictor import Prediction


@dataclass
class Alert:
    level: str      # "info" | "warning" | "critical"
    message: str


def check_invalidation(prediction: Prediction, last_price: float,
                        buffer_pct: float = 0.5) -> list[Alert]:
    alerts: list[Alert] = []
    inv = prediction.invalidation_level
    direction = 1 if prediction.wave_count.legs.p1 > prediction.wave_count.legs.p0 else -1

    distance_pct = abs(last_price - inv) / inv * 100 if inv else 0

    broken = (last_price < inv) if direction > 0 else (last_price > inv)

    if broken:
        alerts.append(Alert(
            "critical",
            f"Wave count INVALIDATED: price {last_price:.2f} has broken the "
            f"invalidation level {inv:.2f}. {prediction.invalidation_note} "
            "Re-run detection — the market has likely relabeled into a "
            "different structure.",
        ))
    elif distance_pct <= buffer_pct:
        alerts.append(Alert(
            "warning",
            f"Price {last_price:.2f} is within {distance_pct:.2f}% of the "
            f"invalidation level {inv:.2f}. Early warning: this count may "
            "break soon.",
        ))
    else:
        alerts.append(Alert("info", "No invalidation risk detected."))

    return alerts


def check_sudden_move(df: pd.DataFrame, atr_period: int = 14,
                       atr_multiple: float = 3.0) -> list[Alert]:
    """Flag the most recent bar if its true range is an outlier vs ATR."""
    if len(df) < atr_period + 2:
        return []

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(atr_period).mean()
    last_tr = tr.iloc[-1]
    last_atr = atr.iloc[-2]  # ATR prior to the bar being tested

    alerts: list[Alert] = []
    if last_atr and last_tr >= atr_multiple * last_atr:
        alerts.append(Alert(
            "warning",
            f"Sudden move detected: latest bar range ({last_tr:.2f}) is "
            f"{last_tr / last_atr:.1f}x the recent ATR ({last_atr:.2f}). "
            "Sharp moves like this often precede a wave-count change — "
            "re-check the count.",
        ))
    return alerts
