from src.portfolio import PaperPortfolio, price_at_equity_ratio, risk_band
from src.leverage_manager import Position

TIERS = [(0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50)]


def _fresh_portfolio(cash=100_000.0):
    return PaperPortfolio(
        ticker="TEST", starting_cash=cash,
        maintenance_margin_ratio=0.25, buffer_start_ratio=0.45, tiers=TIERS,
    )


# --- basic open/close correctness --------------------------------------

def test_open_long_deducts_margin():
    pf = _fresh_portfolio()
    result = pf.open("buy", notional_usd=10_000, leverage=5.0, mark_price=100.0)
    assert result.ok
    assert pf.cash == 100_000 - 2_000  # margin = notional/leverage
    assert pf.position.size == 100.0    # 10000/100
    assert pf.position.is_long is True


def test_open_short():
    pf = _fresh_portfolio()
    result = pf.open("sell", notional_usd=10_000, leverage=5.0, mark_price=100.0)
    assert result.ok
    assert pf.position.is_long is False


def test_cannot_open_second_position_while_one_is_open():
    pf = _fresh_portfolio()
    pf.open("buy", notional_usd=10_000, leverage=5.0, mark_price=100.0)
    result = pf.open("buy", notional_usd=5_000, leverage=3.0, mark_price=100.0)
    assert not result.ok
    assert "close it" in result.message.lower()


def test_insufficient_cash_rejected():
    pf = _fresh_portfolio(cash=1_000.0)
    result = pf.open("buy", notional_usd=10_000, leverage=2.0, mark_price=100.0)  # needs $5000 margin
    assert not result.ok
    assert "insufficient" in result.message.lower()


def test_manual_close_realizes_correct_pnl():
    pf = _fresh_portfolio()
    pf.open("buy", notional_usd=10_000, leverage=5.0, mark_price=100.0)  # 100 shares, $2000 margin
    pf.mark_to_market(110.0)  # +10% move, healthy, no tier action
    result = pf.close(110.0)
    assert result.ok
    # pnl = (110-100)*100 = 1000; cash = 100000 - 2000 (margin) + 2000 (margin back) + 1000 (pnl)
    assert abs(pf.cash - 101_000.0) < 1e-6
    assert pf.position is None
    assert abs(pf.realized_pnl - 1000.0) < 1e-6


def test_short_pnl_direction():
    pf = _fresh_portfolio()
    pf.open("sell", notional_usd=10_000, leverage=5.0, mark_price=100.0)  # short 100 shares
    pf.mark_to_market(90.0)  # price fell -> short profits
    result = pf.close(90.0)
    assert result.ok
    # pnl = (100-90)*100 = 1000 for a short
    assert abs(pf.realized_pnl - 1000.0) < 1e-6


# --- dynamic buffer auto-deleveraging wired to a real position ---------

def test_falling_price_triggers_staged_deleveraging():
    pf = _fresh_portfolio()
    pf.open("buy", notional_usd=50_000, leverage=5.0, mark_price=100.0)  # 500 shares
    original_size = pf.position.size

    # Walk price down through tier bands (mirrors the --demo-leverage grid)
    msgs_by_price = {}
    for price in [97, 94, 91, 88.4, 86.8, 85.6, 82, 80]:
        msgs = pf.mark_to_market(price)
        msgs_by_price[price] = msgs

    # Tiers should have fired at 88.4 (10%), 86.8 (15%), 85.6 (25%), 82 (50%)
    assert len(msgs_by_price[88.4]) == 1
    assert len(msgs_by_price[86.8]) == 1
    assert len(msgs_by_price[85.6]) == 1
    assert len(msgs_by_price[82]) == 1
    # size should have shrunk with each tier, well below the original
    assert pf.position is None or pf.position.size < original_size
    # audit log should have a matching number of entries (plus the open entry)
    assert len(pf.audit_log) >= 5  # 1 open + 4 tier actions (position may be gone by 80)


def test_full_wipeout_clears_position_and_releases_cash():
    pf = _fresh_portfolio()
    pf.open("buy", notional_usd=50_000, leverage=5.0, mark_price=100.0)
    pf.mark_to_market(80.0)  # ratio ~0 at 5x leverage -> full liquidation
    assert pf.position is None
    # cash should reflect margin release net of losses, never negative
    assert pf.cash >= 0


def test_no_position_no_action():
    pf = _fresh_portfolio()
    msgs = pf.mark_to_market(100.0)
    assert msgs == []


# --- price_at_equity_ratio inverse-correctness --------------------------

def test_price_at_equity_ratio_matches_forward_calc():
    entry, leverage = 100.0, 5.0
    for is_long in (True, False):
        for target_ratio in (0.45, 0.38, 0.31, 0.25, 0.0):
            price = price_at_equity_ratio(entry, leverage, is_long, target_ratio)
            pos = Position(ticker="X", size=10, entry_price=entry,
                            leverage=leverage, mark_price=price, is_long=is_long)
            computed_ratio = pos.equity_ratio()
            assert abs(computed_ratio - target_ratio) < 1e-6, (is_long, target_ratio, computed_ratio, price)


# --- risk_band -----------------------------------------------------------

def test_risk_band_thresholds():
    assert risk_band(None) == "No Position"
    assert risk_band(0.9) == "Low"
    assert risk_band(0.5) == "Moderate"
    assert risk_band(0.40) == "High"
    assert risk_band(0.30) == "Red"
    assert risk_band(0.10) == "Extreme"


# --- to_dict shape (what the dashboard actually consumes) ---------------

def test_to_dict_no_position_shape():
    pf = _fresh_portfolio()
    d = pf.to_dict(mark_price=None)
    assert d["has_position"] is False
    assert d["risk_level"] == "No Position"
    assert "cash" in d and "audit_log" in d


def test_to_dict_with_position_shape():
    pf = _fresh_portfolio()
    pf.open("buy", notional_usd=10_000, leverage=5.0, mark_price=100.0)
    pf.mark_to_market(105.0)
    d = pf.to_dict(mark_price=105.0)
    assert d["has_position"] is True
    assert d["side"] == "long"
    assert d["liquidation_price"] < 100.0  # long liquidation is below entry
    assert len(d["tier_levels"]) == 4
    assert d["risk_level"] in ("Low", "Moderate", "High", "Red", "Extreme")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
