"""
Paper trading portfolio, wired directly to leverage_manager's
LiquidationEngine so opening a leveraged position here and watching the
live price move actually triggers the staged de-leveraging described in
leverage_manager.py — not a separate demo of the logic, the same engine
acting on a real (paper) position.

Deliberately simple on purpose, for reliability in a live demo:
  - one open position per portfolio at a time (long OR short, not both)
  - must fully close before opening a new/opposite position
  - market orders only, filled at the current mark price
No real money, no real orders — this never touches a broker.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .leverage_manager import Position, LiquidationEngine


@dataclass
class AuditEntry:
    ts: float
    level: str      # "info" | "warning" | "critical"
    message: str


@dataclass
class OrderResult:
    ok: bool
    message: str


def price_at_equity_ratio(entry_price: float, leverage: float, is_long: bool,
                           ratio: float) -> float:
    """
    Inverse of Position.equity_ratio(): the mark price at which a position
    with this entry/leverage/direction would hit the given equity ratio.
    Used to draw tier/liquidation price *lines* on the chart ahead of time,
    rather than only knowing them after the fact.
    """
    direction = 1 if is_long else -1
    # ratio = 1 + leverage*direction*(mark/entry - 1)  =>  solve for mark
    return entry_price * (1 + (ratio - 1) / (leverage * direction))


RISK_BANDS = [
    # (ratio_floor_exclusive, label)  — checked top-down, first match wins
    (0.70, "Low"),
    (0.45, "Moderate"),
    (0.38, "High"),
    (0.25, "Red"),
    (float("-inf"), "Extreme"),
]


def risk_band(ratio: Optional[float]) -> str:
    if ratio is None:
        return "No Position"
    for floor, label in RISK_BANDS:
        if ratio > floor:
            return label
    return "Extreme"


class PaperPortfolio:
    def __init__(self, ticker: str, starting_cash: float,
                 maintenance_margin_ratio: float, buffer_start_ratio: float,
                 tiers: list[tuple[float, float]]):
        self.ticker = ticker
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.position: Optional[Position] = None
        self.realized_pnl = 0.0
        self.maintenance_margin_ratio = maintenance_margin_ratio
        self.buffer_start_ratio = buffer_start_ratio
        self.tiers = tiers
        self._engine = LiquidationEngine(maintenance_margin_ratio, buffer_start_ratio, tiers)
        self.audit_log: list[AuditEntry] = []

    def _log(self, level: str, message: str) -> None:
        self.audit_log.append(AuditEntry(ts=time.time(), level=level, message=message))
        self.audit_log = self.audit_log[-100:]  # bounded

    def open(self, side: str, notional_usd: float, leverage: float,
              mark_price: float) -> OrderResult:
        if side not in ("buy", "sell"):
            return OrderResult(False, "side must be 'buy' or 'sell'")
        if self.position is not None and self.position.size > 0:
            return OrderResult(False, "A position is already open — close it before opening a new one.")
        if notional_usd <= 0:
            return OrderResult(False, "notional_usd must be positive")
        if leverage < 1:
            return OrderResult(False, "leverage must be >= 1")
        margin_required = notional_usd / leverage
        if margin_required > self.cash:
            return OrderResult(False, f"Insufficient cash: need ${margin_required:,.2f} margin, have ${self.cash:,.2f}")

        size = notional_usd / mark_price
        is_long = (side == "buy")
        self.cash -= margin_required
        self.position = Position(
            ticker=self.ticker, size=size, entry_price=mark_price,
            leverage=leverage, mark_price=mark_price, is_long=is_long,
        )
        self._engine.reset(self.ticker)
        self._log("info",
                   f"Opened {'LONG' if is_long else 'SHORT'} {size:.6f} {self.ticker} "
                   f"@ ${mark_price:,.2f} ({leverage:.1f}x, ${margin_required:,.2f} margin posted).")
        return OrderResult(True, "Position opened.")

    def close(self, mark_price: float) -> OrderResult:
        if self.position is None or self.position.size <= 0:
            return OrderResult(False, "No open position to close.")
        self._close_fraction(1.0, mark_price, reason="Manually closed by user.", level="info")
        return OrderResult(True, "Position closed.")

    def _close_fraction(self, fraction: float, mark_price: float, reason: str, level: str) -> float:
        """Realize P&L on `fraction` of the current position; returns $ realized (incl. margin release)."""
        pos = self.position
        shares = pos.size * fraction
        direction = 1 if pos.is_long else -1
        margin_before = pos.initial_margin
        margin_released = margin_before * fraction
        pnl = (mark_price - pos.entry_price) * shares * direction

        self.cash += margin_released + pnl
        self.realized_pnl += pnl
        pos.size -= shares

        self._log(level, f"{reason} Closed {shares:.6f} {self.ticker} ({fraction*100:.0f}% of position) "
                          f"@ ${mark_price:,.2f} — realized P&L ${pnl:,.2f}.")

        if pos.size <= 1e-9:
            self.position = None
            self._engine.reset(self.ticker)

        return margin_released + pnl

    def mark_to_market(self, mark_price: float) -> list[str]:
        """Update the open position's mark price, run the dynamic-buffer
        liquidation engine against it, and apply any tier actions it fires.
        Returns a list of human-readable messages for actions taken this tick."""
        if self.position is None or self.position.size <= 0:
            return []

        self.position.mark_price = mark_price
        plan = self._engine.evaluate(self.position)
        messages = []
        for action in plan.actions:
            fraction = action.close_fraction
            level = "critical" if plan.fully_liquidated else "warning"
            self._close_fraction(fraction, mark_price, reason=action.reason, level=level)
            messages.append(action.reason)
        return messages

    def to_dict(self, mark_price: Optional[float]) -> dict:
        if self.position is None or self.position.size <= 0:
            return {
                "has_position": False,
                "cash": round(self.cash, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "total_equity": round(self.cash, 2),
                "risk_level": "No Position",
                "audit_log": [
                    {"ts": e.ts, "level": e.level, "message": e.message}
                    for e in reversed(self.audit_log[-20:])
                ],
            }

        pos = self.position
        ratio = pos.equity_ratio()
        direction = 1 if pos.is_long else -1
        unrealized_pnl = (mark_price - pos.entry_price) * pos.size * direction if mark_price else 0.0
        position_equity = pos.initial_margin + unrealized_pnl
        total_equity = self.cash + position_equity

        # Price levels for chart annotation: terminal liquidation line + each
        # buffer tier's trigger price, computed ahead of time from the
        # position's entry/leverage — not just discovered after the fact.
        tier_levels = []
        for floor, close_fraction in sorted(self.tiers, key=lambda t: -t[0]):
            price = price_at_equity_ratio(pos.entry_price, pos.leverage, pos.is_long, floor)
            tier_levels.append({
                "ratio_floor": floor, "close_fraction": close_fraction,
                "price": round(price, 6),
            })
        liquidation_price = price_at_equity_ratio(
            pos.entry_price, pos.leverage, pos.is_long, self.maintenance_margin_ratio
        )
        buffer_start_price = price_at_equity_ratio(
            pos.entry_price, pos.leverage, pos.is_long, self.buffer_start_ratio
        )

        return {
            "has_position": True,
            "side": "long" if pos.is_long else "short",
            "size": pos.size,
            "entry_price": pos.entry_price,
            "leverage": pos.leverage,
            "mark_price": mark_price,
            "equity_ratio": round(ratio, 4),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round((unrealized_pnl / pos.initial_margin) * 100, 2) if pos.initial_margin else 0.0,
            "cash": round(self.cash, 2),
            "position_equity": round(position_equity, 2),
            "total_equity": round(total_equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "risk_level": risk_band(ratio),
            "liquidation_price": round(liquidation_price, 6),
            "buffer_start_price": round(buffer_start_price, 6),
            "tier_levels": tier_levels,
            "audit_log": [
                {"ts": e.ts, "level": e.level, "message": e.message}
                for e in reversed(self.audit_log[-20:])
            ],
        }
