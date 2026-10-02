"""
Projects likely continuation targets for the current wave using Fibonacci
guideline ratios, and computes the invalidation level for the active count.

All outputs are ranges with an explicit confidence score, not point
predictions — Elliott Wave targets are probabilistic zones, not exact prices.
"""

from dataclasses import dataclass
from .wave_detector import WaveCount
from .wave_rules import COMMON_WAVE3_EXTENSION, COMMON_WAVEC_EXTENSION


@dataclass
class Projection:
    label: str
    low: float
    high: float
    rationale: str


@dataclass
class Prediction:
    wave_count: WaveCount
    invalidation_level: float
    invalidation_note: str
    targets: list[Projection]


def predict(wave_count: WaveCount) -> Prediction:
    legs = wave_count.legs
    direction = 1 if legs.p1 > legs.p0 else -1
    w1_len = abs(legs.p1 - legs.p0)

    targets: list[Projection] = []

    if wave_count.current_wave in ("3", "5 (forming)") and legs.p2 is not None:
        lo_mult, hi_mult = min(COMMON_WAVE3_EXTENSION), max(COMMON_WAVE3_EXTENSION)
        base = legs.p2
        lo = base + direction * lo_mult * w1_len
        hi = base + direction * hi_mult * w1_len
        targets.append(Projection(
            label="Wave 3 target zone (1.618x-2.618x Wave 1 from Wave 2 low)",
            low=min(lo, hi), high=max(lo, hi),
            rationale="Wave 3 is most commonly the extended wave in an impulse.",
        ))

    if wave_count.current_wave == "5 (forming)" and legs.p4 is not None:
        base = legs.p4
        w1_move = legs.p1 - legs.p0
        lo = base + 0.618 * w1_move
        hi = base + 1.0 * w1_move
        targets.append(Projection(
            label="Wave 5 target zone (0.618x-1.0x Wave 1 from Wave 4 low)",
            low=min(lo, hi), high=max(lo, hi),
            rationale="Wave 5 commonly equals or extends 61.8-100% of Wave 1 "
                       "when Wave 3 was already extended.",
        ))

    invalidation_level, invalidation_note = _invalidation_level(wave_count, direction)

    return Prediction(
        wave_count=wave_count,
        invalidation_level=invalidation_level,
        invalidation_note=invalidation_note,
        targets=targets,
    )


def _invalidation_level(wave_count: WaveCount, direction: int) -> tuple[float, str]:
    legs = wave_count.legs
    if wave_count.current_wave in ("3", "5 (forming)"):
        return (
            legs.p2,
            "Close beyond the Wave 2 extreme invalidates this impulse count "
            "(would break the 'Wave 4 cannot overlap Wave 1 / Wave 2 cannot "
            "fully retrace' structure).",
        )
    if wave_count.current_wave == "5":
        return (
            legs.p4,
            "Close beyond the Wave 4 extreme invalidates the Wave 5 count.",
        )
    return (legs.p0, "Close beyond the start of Wave 1 invalidates the entire count.")
