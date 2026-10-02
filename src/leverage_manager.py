"""
Dynamic buffer liquidation.

Instead of a single full liquidation at the maintenance-margin line, this
module closes a leveraged position in stages as its equity ratio degrades
through configured tiers. This is a risk-simulation module: it takes
position state and a mark price, and returns liquidation *actions* — it
does not place real orders. Wire the actions into your broker/execution
layer if you want it to act live, and paper-trade it first.

equity_ratio = equity / initial_margin_posted (see Position.equity_ratio()).
1.0 = no unrealized loss yet; 0.0 = the margin originally posted is wiped
out; the tier floors in config.yaml (0.45, 0.38, 0.31, 0.25...) are
calibrated against this scale, not against total position value — higher
leverage means the ratio moves faster per % price move, which is exactly
why higher-leverage positions need buffer tiers spaced with that in mind.
"""

from dataclasses import dataclass, field


@dataclass
class Position:
    ticker: str
    size: float          # number of shares/contracts currently held
    entry_price: float
    leverage: float       # e.g. 5.0 for 5x
    mark_price: float     # current market price
    is_long: bool = True

    @property
    def position_value(self) -> float:
        return self.size * self.mark_price

    @property
    def initial_margin(self) -> float:
        """Capital originally put up: notional at entry, divided by leverage."""
        return self.size * self.entry_price / self.leverage

    def equity_ratio(self) -> float:
        """
        Equity as a fraction of the *initial margin put up* (not of position
        value). This is what the tier floors in config.yaml are calibrated
        against: 1.0 = fully healthy (no unrealized loss), 0.0 = initial
        margin wiped out, negative = losses have exceeded the margin posted
        (the "bad debt" zone this module exists to prevent).

        direction = +1 long, -1 short.
        """
        if self.initial_margin <= 0:
            return 0.0
        direction = 1 if self.is_long else -1
        pct_move = (self.mark_price / self.entry_price) - 1
        return 1 + self.leverage * direction * pct_move


@dataclass
class LiquidationAction:
    ticker: str
    close_fraction: float   # fraction of *current remaining* size to close
    shares_to_close: float
    reason: str
    tier_ratio: float


@dataclass
class LiquidationPlan:
    position: Position
    equity_ratio: float
    actions: list[LiquidationAction] = field(default_factory=list)
    fully_liquidated: bool = False


def evaluate_position(position: Position, maintenance_margin_ratio: float,
                       buffer_start_ratio: float,
                       tiers: list[tuple[float, float]]) -> LiquidationPlan:
    """
    tiers: list of (equity_ratio_floor, close_fraction), sorted descending by
    floor (as in config.yaml). The first tier whose floor the current equity
    ratio has crossed (and hasn't already been actioned) fires.

    This function is stateless per call — in a real system you'd track which
    tiers have already fired for this position (persisted state) so the same
    tier doesn't re-trigger every tick. See `LiquidationEngine` below for a
    stateful wrapper that handles that.
    """
    ratio = position.equity_ratio()
    plan = LiquidationPlan(position=position, equity_ratio=ratio)

    # Tolerance guards against floating-point noise (e.g. a "true" zero
    # equity ratio landing on 2e-16 instead of exactly 0) letting a wiped-out
    # position slip past this check — which matters here specifically
    # because it's the one branch this module must never miss.
    if ratio <= 1e-9:
        plan.actions.append(LiquidationAction(
            ticker=position.ticker, close_fraction=1.0,
            shares_to_close=position.size,
            reason="Equity exhausted — full liquidation to prevent negative "
                   "balance / bad debt.",
            tier_ratio=0.0,
        ))
        plan.fully_liquidated = True
        return plan

    if ratio > buffer_start_ratio:
        return plan  # healthy — no action

    # Check tiers from most severe (lowest floor) to least severe, so the
    # deepest crossed tier fires — not just the first/shallowest one whose
    # floor the ratio happens to be under.
    for floor, close_fraction in sorted(tiers, key=lambda t: t[0]):
        if ratio <= floor:
            shares = position.size * close_fraction
            plan.actions.append(LiquidationAction(
                ticker=position.ticker,
                close_fraction=close_fraction,
                shares_to_close=round(shares, 6),
                reason=f"Equity ratio {ratio:.3f} crossed tier floor {floor:.3f}: "
                       f"closing {close_fraction * 100:.0f}% of remaining position "
                       "to de-risk gradually instead of full liquidation.",
                tier_ratio=floor,
            ))
            break  # only the deepest crossed tier fires per evaluation

    if ratio <= maintenance_margin_ratio and not plan.actions:
        # safety net: hard line reached but no tier matched (misconfigured
        # tiers) -> force full liquidation rather than leave the account
        # under-collateralized.
        plan.actions.append(LiquidationAction(
            ticker=position.ticker, close_fraction=1.0,
            shares_to_close=position.size,
            reason="Maintenance margin line reached with no matching buffer "
                   "tier — forcing full liquidation as a safety net.",
            tier_ratio=maintenance_margin_ratio,
        ))
        plan.fully_liquidated = True

    return plan


class LiquidationEngine:
    """
    Stateful wrapper: tracks which tier floor has already fired per ticker so
    repeated evaluate() calls (e.g. on every price tick) don't re-trigger the
    same tier over and over as the ratio hovers near a floor.
    """

    def __init__(self, maintenance_margin_ratio: float, buffer_start_ratio: float,
                 tiers: list[tuple[float, float]]):
        self.maintenance_margin_ratio = maintenance_margin_ratio
        self.buffer_start_ratio = buffer_start_ratio
        self.tiers = tiers
        self._fired_floors: dict[str, set[float]] = {}

    def evaluate(self, position: Position) -> LiquidationPlan:
        plan = evaluate_position(
            position, self.maintenance_margin_ratio,
            self.buffer_start_ratio, self.tiers,
        )
        fired = self._fired_floors.setdefault(position.ticker, set())
        plan.actions = [a for a in plan.actions if a.tier_ratio not in fired]
        for a in plan.actions:
            fired.add(a.tier_ratio)

        # Safety net that only the stateful engine can correctly apply: if
        # equity is at/below the hard maintenance line but this tick's action
        # got de-duped away (its tier already fired on an earlier tick), the
        # remaining position is still sitting below the maintenance line
        # un-actioned. Force full liquidation rather than leave it exposed.
        if (not plan.actions and not plan.fully_liquidated
                and plan.equity_ratio <= self.maintenance_margin_ratio
                and position.size > 0):
            action = LiquidationAction(
                ticker=position.ticker, close_fraction=1.0,
                shares_to_close=position.size,
                reason="Equity ratio remains at/below the maintenance margin "
                       "line and every applicable buffer tier has already "
                       "fired — forcing full liquidation of the remainder "
                       "rather than leaving it exposed below the hard line.",
                tier_ratio=self.maintenance_margin_ratio,
            )
            plan.actions = [action]
            plan.fully_liquidated = True
            fired.add(self.maintenance_margin_ratio)

        return plan

    def reset(self, ticker: str) -> None:
        """Call after a position is closed/reopened to clear fired-tier state."""
        self._fired_floors.pop(ticker, None)
