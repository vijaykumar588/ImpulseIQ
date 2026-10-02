"""
Elliott Wave Theory — rule constants and objective (hard) rule checks.

Elliott Wave splits into:
  - HARD RULES: never broken in a valid count. If violated, the count is
    invalid, full stop. These are implemented as boolean checks below.
  - GUIDELINES: usually true, not always (Fibonacci ratios, alternation,
    channeling). These feed the *confidence score* in predictor.py rather
    than invalidating a count outright.

An impulse wave is a 5-wave move (1-2-3-4-5) in the direction of the larger
trend. A correction is a 3-wave move (A-B-C) against it.
"""

from dataclasses import dataclass
from enum import Enum


class WaveType(Enum):
    IMPULSE = "impulse"
    CORRECTIVE = "corrective"


# --- Hard rules (objective, never violated in a valid Elliott count) -------

def rule_wave2_no_full_retrace(w1_start: float, w1_end: float, w2_end: float) -> bool:
    """Wave 2 can never retrace more than 100% of Wave 1."""
    if w1_end > w1_start:  # uptrend impulse
        return w2_end > w1_start
    return w2_end < w1_start  # downtrend impulse


def rule_wave3_not_shortest(w1_len: float, w3_len: float, w5_len: float) -> bool:
    """Wave 3 is never the shortest of waves 1, 3, and 5."""
    return w3_len >= min(w1_len, w5_len)


def rule_wave4_no_overlap(w1_end: float, w4_end: float, direction: int,
                           tolerance_pct: float = 0.0) -> bool:
    """
    Wave 4 may not enter the price territory of Wave 1 (standard/strict
    Elliott Wave; some practitioners allow minor overlap in diagonals —
    tolerance_pct lets you loosen this if you intend to detect diagonals).
    direction: +1 for uptrend impulse, -1 for downtrend impulse.
    """
    tol = w1_end * (tolerance_pct / 100.0)
    if direction > 0:
        return w4_end >= (w1_end - tol)
    return w4_end <= (w1_end + tol)


def rule_wave3_minimum_extension(w1_len: float, w3_len: float,
                                  min_multiple: float = 1.0) -> bool:
    """Wave 3 must be at least `min_multiple` times the length of Wave 1."""
    return w3_len >= (min_multiple * w1_len)


@dataclass
class ImpulseLegs:
    """Price levels for a candidate 5-wave impulse, in order."""
    p0: float  # start of wave 1
    p1: float  # end of wave 1 / start of wave 2
    p2: float  # end of wave 2 / start of wave 3
    p3: float  # end of wave 3 / start of wave 4
    p4: float  # end of wave 4 / start of wave 5
    p5: float | None = None  # end of wave 5 (optional — may still be forming)


def validate_impulse(legs: ImpulseLegs, wave4_overlap_tolerance_pct: float = 0.0,
                      min_wave3_extension: float = 1.0) -> tuple[bool, list[str]]:
    """
    Check a candidate impulse against all hard rules. Returns (is_valid,
    list_of_violated_rule_descriptions). An empty violation list means the
    count is structurally valid so far (waves not yet formed are skipped).
    """
    violations = []
    direction = 1 if legs.p1 > legs.p0 else -1

    w1_len = abs(legs.p1 - legs.p0)
    w3_len = abs(legs.p3 - legs.p2)

    if not rule_wave2_no_full_retrace(legs.p0, legs.p1, legs.p2):
        violations.append("Wave 2 retraced more than 100% of Wave 1")

    if not rule_wave3_minimum_extension(w1_len, w3_len, min_wave3_extension):
        violations.append(
            f"Wave 3 shorter than {min_wave3_extension}x Wave 1 (min extension rule)"
        )

    if not rule_wave4_no_overlap(legs.p1, legs.p4, direction, wave4_overlap_tolerance_pct):
        violations.append("Wave 4 overlapped Wave 1 territory")

    if legs.p5 is not None:
        w5_len = abs(legs.p5 - legs.p4)
        if not rule_wave3_not_shortest(w1_len, w3_len, w5_len):
            violations.append("Wave 3 is the shortest of waves 1/3/5")

    return (len(violations) == 0, violations)


# --- Fibonacci guideline ratios (probabilistic, used for scoring/targets) --

FIB_RETRACEMENTS = [0.236, 0.382, 0.5, 0.618, 0.786]
FIB_EXTENSIONS = [0.618, 1.0, 1.272, 1.618, 2.0, 2.618]

COMMON_WAVE2_RETRACE = (0.5, 0.618, 0.786)   # Wave 2 often deep
COMMON_WAVE4_RETRACE = (0.236, 0.382)         # Wave 4 often shallow (alternation)
COMMON_WAVE3_EXTENSION = (1.618, 2.618)       # Wave 3 vs Wave 1
COMMON_WAVEC_EXTENSION = (1.0, 1.618)         # Wave C vs Wave A
