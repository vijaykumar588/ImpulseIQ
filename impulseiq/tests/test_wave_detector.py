import pandas as pd
import pytest

from src.wave_rules import ImpulseLegs, validate_impulse
from src.wave_detector import find_zigzag_pivots, label_current_wave, Pivot
from src.predictor import predict
from src.leverage_manager import Position, evaluate_position, LiquidationEngine


# --- wave_rules --------------------------------------------------------

def test_valid_impulse_passes():
    legs = ImpulseLegs(p0=100, p1=110, p2=105, p3=125, p4=118, p5=135)
    ok, violations = validate_impulse(legs)
    assert ok
    assert violations == []


def test_wave2_full_retrace_is_invalid():
    legs = ImpulseLegs(p0=100, p1=110, p2=99, p3=125, p4=118, p5=135)
    ok, violations = validate_impulse(legs)
    assert not ok
    assert any("Wave 2" in v for v in violations)


def test_wave4_overlap_is_invalid():
    legs = ImpulseLegs(p0=100, p1=110, p2=105, p3=125, p4=109, p5=135)
    ok, violations = validate_impulse(legs)
    assert not ok
    assert any("Wave 4" in v for v in violations)


def test_wave3_shortest_is_invalid():
    legs = ImpulseLegs(p0=100, p1=110, p2=105, p3=108, p4=106, p5=112)
    ok, violations = validate_impulse(legs)
    assert not ok
    assert any("Wave 3" in v for v in violations)


# --- wave_detector -------------------------------------------------------

def _make_price_series(prices, start="2024-01-01"):
    dates = pd.date_range(start, periods=len(prices), freq="D")
    return pd.DataFrame({
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1000] * len(prices),
    }, index=dates)


def test_zigzag_detects_clear_swings():
    # Big obvious up-down-up-down-up swings, well above threshold
    prices = [100, 130, 105, 150, 120, 170]
    df = _make_price_series(prices)
    pivots = find_zigzag_pivots(df, threshold_pct=4.0)
    assert len(pivots) >= 5
    assert isinstance(pivots[0], Pivot)


def test_label_current_wave_needs_min_pivots():
    pivots = [Pivot(pd.Timestamp("2024-01-01"), 100, "low"),
              Pivot(pd.Timestamp("2024-01-02"), 110, "high")]
    assert label_current_wave(pivots) is None


def test_label_current_wave_valid_structure():
    dates = pd.date_range("2024-01-01", periods=6, freq="D")
    prices = [100, 110, 105, 125, 118, 135]
    pivots = [Pivot(d, p, "low" if i % 2 == 0 else "high")
              for i, (d, p) in enumerate(zip(dates, prices))]
    wc = label_current_wave(pivots)
    assert wc is not None
    assert wc.is_valid
    assert 0.0 <= wc.confidence <= 1.0


# --- predictor -------------------------------------------------------------

def test_predict_returns_invalidation_and_targets():
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    prices = [100, 110, 105, 125, 118]
    pivots = [Pivot(d, p, "low" if i % 2 == 0 else "high")
              for i, (d, p) in enumerate(zip(dates, prices))]
    wc = label_current_wave(pivots)
    pred = predict(wc)
    assert pred.invalidation_level == 105  # Wave 2 low
    assert len(pred.targets) >= 1
    for t in pred.targets:
        assert t.low <= t.high


# --- leverage_manager --------------------------------------------------

def test_healthy_position_no_action():
    pos = Position(ticker="X", size=100, entry_price=100, leverage=5.0,
                    mark_price=105, is_long=True)
    plan = evaluate_position(pos, maintenance_margin_ratio=0.25,
                              buffer_start_ratio=0.45,
                              tiers=[(0.45, 0.10), (0.25, 0.5)])
    assert plan.actions == []


def test_tier_fires_as_equity_degrades():
    # 5x leverage, ~13% adverse move -> equity_ratio ~0.35, inside the
    # 0.38 tier but above the 0.31/0.25 tiers -> exactly one tier fires.
    pos = Position(ticker="X", size=1000, entry_price=100, leverage=5.0,
                    mark_price=87, is_long=True)
    tiers = [(0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50)]
    plan = evaluate_position(pos, maintenance_margin_ratio=0.25,
                              buffer_start_ratio=0.45, tiers=tiers)
    assert len(plan.actions) == 1
    assert plan.actions[0].shares_to_close > 0
    assert not plan.fully_liquidated


def test_zero_equity_forces_full_liquidation():
    pos = Position(ticker="X", size=1000, entry_price=100, leverage=5.0,
                    mark_price=79, is_long=True)
    tiers = [(0.45, 0.10), (0.25, 0.5)]
    plan = evaluate_position(pos, maintenance_margin_ratio=0.25,
                              buffer_start_ratio=0.45, tiers=tiers)
    assert plan.fully_liquidated
    assert plan.actions[0].close_fraction == 1.0


def test_engine_does_not_refire_same_tier_twice():
    # mark_price chosen so equity_ratio (~0.42) sits inside the shallowest
    # tier band, comfortably above the maintenance line (0.25) — so the
    # de-dup behavior is isolated from the separate maintenance-line safety
    # net (see test_engine_forces_full_liquidation_below_maintenance_line).
    tiers = [(0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50)]
    engine = LiquidationEngine(maintenance_margin_ratio=0.25,
                                buffer_start_ratio=0.45, tiers=tiers)
    pos = Position(ticker="X", size=1000, entry_price=100, leverage=5.0,
                    mark_price=88.4, is_long=True)

    plan1 = engine.evaluate(pos)
    assert len(plan1.actions) == 1
    for a in plan1.actions:
        pos.size -= a.shares_to_close

    # same price tick again -> same tier should NOT re-fire
    plan2 = engine.evaluate(pos)
    assert len(plan2.actions) == 0


def test_engine_forces_full_liquidation_below_maintenance_line():
    # Once equity sits at/below the maintenance line and the deepest buffer
    # tier has already fired once, leaving the remainder open indefinitely
    # is exactly the bad-debt exposure this module exists to prevent — the
    # engine should force a full liquidation on the next tick rather than
    # silently do nothing because the tier was already "used".
    tiers = [(0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50)]
    engine = LiquidationEngine(maintenance_margin_ratio=0.25,
                                buffer_start_ratio=0.45, tiers=tiers)
    pos = Position(ticker="X", size=1000, entry_price=100, leverage=5.0,
                    mark_price=85, is_long=True)

    plan1 = engine.evaluate(pos)
    assert len(plan1.actions) == 1
    for a in plan1.actions:
        pos.size -= a.shares_to_close
    assert not plan1.fully_liquidated

    plan2 = engine.evaluate(pos)
    assert len(plan2.actions) == 1
    assert plan2.fully_liquidated
    assert plan2.actions[0].close_fraction == 1.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
