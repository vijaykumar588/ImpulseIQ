"""
Pivot (zigzag) detection + Elliott Wave labeling.

Approach:
  1. Reduce OHLCV close series to a zigzag of significant swing highs/lows
     (filters out noise below `threshold_pct`).
  2. Slide a 6-point window (p0..p5) across the pivots and test each
     candidate against the hard Elliott Wave rules (wave_rules.py).
  3. Return the most recent valid (or best-fit) impulse candidate as the
     "current wave count", along with which wave is presently unfolding.
"""

from dataclasses import dataclass
import pandas as pd

from .wave_rules import ImpulseLegs, validate_impulse


@dataclass
class Pivot:
    date: pd.Timestamp
    price: float
    kind: str  # "high" or "low"


def find_zigzag_pivots(df: pd.DataFrame, threshold_pct: float = 4.0) -> list[Pivot]:
    """
    Classic zigzag: track running extreme; confirm a pivot once price
    reverses by >= threshold_pct from that extreme.
    """
    if df.empty:
        return []

    closes = df["close"]
    pivots: list[Pivot] = []

    trend = None  # "up" or "down"
    extreme_price = closes.iloc[0]
    extreme_date = closes.index[0]

    for date, price in closes.items():
        if trend is None:
            if price >= extreme_price * (1 + threshold_pct / 100):
                trend = "up"
                pivots.append(Pivot(extreme_date, extreme_price, "low"))
                extreme_price, extreme_date = price, date
            elif price <= extreme_price * (1 - threshold_pct / 100):
                trend = "down"
                pivots.append(Pivot(extreme_date, extreme_price, "high"))
                extreme_price, extreme_date = price, date
            else:
                if price > extreme_price:
                    extreme_price, extreme_date = price, date
                elif price < extreme_price:
                    pass
            continue

        if trend == "up":
            if price > extreme_price:
                extreme_price, extreme_date = price, date
            elif price <= extreme_price * (1 - threshold_pct / 100):
                pivots.append(Pivot(extreme_date, extreme_price, "high"))
                trend = "down"
                extreme_price, extreme_date = price, date
        else:  # trend == "down"
            if price < extreme_price:
                extreme_price, extreme_date = price, date
            elif price >= extreme_price * (1 + threshold_pct / 100):
                pivots.append(Pivot(extreme_date, extreme_price, "low"))
                trend = "up"
                extreme_price, extreme_date = price, date

    # Always include the final extreme as an unconfirmed, in-progress pivot
    last_kind = "high" if trend == "up" else "low"
    pivots.append(Pivot(extreme_date, extreme_price, last_kind))
    return pivots


@dataclass
class WaveCount:
    wave_type: str            # "impulse" (5-wave) so far supported for auto-labeling
    current_wave: str          # e.g. "3", "4", "5 (forming)"
    legs: ImpulseLegs
    pivots: list               # the Pivot objects p0..p4(/p5)
    confidence: float          # 0-1 guideline-based score
    is_valid: bool
    violations: list[str]


def label_current_wave(pivots: list[Pivot], wave4_overlap_tolerance_pct: float = 0.0,
                        min_wave3_extension: float = 1.0) -> WaveCount | None:
    """
    Take the most recent up-to-6 pivots and attempt to label them as an
    impulse (waves 1-5). Returns None if there aren't enough pivots yet.
    """
    if len(pivots) < 5:
        return None

    window = pivots[-6:] if len(pivots) >= 6 else pivots[-5:]
    prices = [p.price for p in window]

    if len(window) == 6:
        legs = ImpulseLegs(*prices)
        current_wave = "5"
    else:
        legs = ImpulseLegs(*prices, p5=None)
        current_wave = "5 (forming)"

    is_valid, violations = validate_impulse(
        legs, wave4_overlap_tolerance_pct, min_wave3_extension
    )

    confidence = _score_confidence(legs)

    return WaveCount(
        wave_type="impulse",
        current_wave=current_wave,
        legs=legs,
        pivots=window,
        confidence=confidence,
        is_valid=is_valid,
        violations=violations,
    )


def _score_confidence(legs: ImpulseLegs) -> float:
    """
    Guideline-based confidence score (0-1). Not a hard rule — reflects how
    closely the candidate matches textbook Fibonacci relationships. Rewards:
      - Wave 2 retracing 50-78.6% of Wave 1
      - Wave 3 extending 1.618-2.618x Wave 1
      - Wave 4 retracing a shallower % than Wave 2 (alternation guideline)
    """
    from .wave_rules import COMMON_WAVE2_RETRACE, COMMON_WAVE3_EXTENSION

    score = 0.4  # baseline for structurally valid count
    w1_len = abs(legs.p1 - legs.p0)
    w2_retrace = abs(legs.p1 - legs.p2) / w1_len if w1_len else 0
    w3_len = abs(legs.p3 - legs.p2)
    w3_ext = w3_len / w1_len if w1_len else 0

    lo, hi = min(COMMON_WAVE2_RETRACE), max(COMMON_WAVE2_RETRACE)
    if lo - 0.05 <= w2_retrace <= hi + 0.05:
        score += 0.25

    lo3, hi3 = min(COMMON_WAVE3_EXTENSION), max(COMMON_WAVE3_EXTENSION)
    if w3_ext >= lo3 * 0.9:
        score += 0.25
        if w3_ext <= hi3 * 1.1:
            score += 0.1

    w4_retrace = abs(legs.p3 - legs.p4) / w3_len if w3_len else 0
    if w4_retrace < w2_retrace:
        score += 0.05  # alternation guideline

    return round(min(score, 1.0), 2)
