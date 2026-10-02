import argparse
import os
import sys

import yaml

from .data_fetcher import get_data_source
from .wave_detector import find_zigzag_pivots, label_current_wave
from .predictor import predict
from .alert_system import check_invalidation, check_sudden_move
from .leverage_manager import Position, LiquidationEngine


def load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return {}


def run_analysis(ticker: str, interval: str, lookback: int, cfg: dict) -> int:
    ds_name = cfg.get("data_source", "yfinance")
    pivot_cfg = cfg.get("pivot_detection", {})
    rules_cfg = cfg.get("wave_rules", {})
    alert_cfg = cfg.get("alerts", {})

    source = get_data_source(ds_name)
    print(f"Fetching {ticker} ({interval}, lookback={lookback}) via {ds_name}...")
    df = source.get_ohlcv(ticker, interval, lookback)

    pivots = find_zigzag_pivots(df, pivot_cfg.get("zigzag_threshold_pct", 4.0))
    print(f"\nTicker: {ticker}  Interval: {interval}")
    print(f"Detected pivots: {len(pivots)}")

    wave_count = label_current_wave(
        pivots,
        wave4_overlap_tolerance_pct=rules_cfg.get("wave4_overlap_tolerance_pct", 0.0),
        min_wave3_extension=rules_cfg.get("min_wave3_extension", 1.0),
    )

    if wave_count is None:
        print("Not enough confirmed pivots yet to label a wave count "
              "(need at least 5). Try a longer lookback or lower threshold.")
        return 0

    status = "VALID" if wave_count.is_valid else "RULE VIOLATION"
    print(f"\nCurrent wave count: Impulse, Wave {wave_count.current_wave} "
          f"of 5  [confidence: {wave_count.confidence}]  ({status})")
    if wave_count.violations:
        for v in wave_count.violations:
            print(f"  - VIOLATION: {v}")

    prediction = predict(wave_count)
    print(f"\nInvalidation level: {prediction.invalidation_level:.2f}")
    print(f"  {prediction.invalidation_note}")

    for t in prediction.targets:
        print(f"\n{t.label}: {t.low:.2f} - {t.high:.2f}")
        print(f"  {t.rationale}")

    last_price = float(df["close"].iloc[-1])
    print(f"\nLast price: {last_price:.2f}")

    alerts = check_invalidation(
        prediction, last_price, alert_cfg.get("invalidation_buffer_pct", 0.5)
    )
    alerts += check_sudden_move(
        df, atr_multiple=alert_cfg.get("sudden_move_atr_multiple", 3.0)
    )
    print("\nAlerts:")
    for a in alerts:
        print(f"  [{a.level.upper()}] {a.message}")

    return 0


def run_leverage_demo(cfg: dict) -> int:
    lm_cfg = cfg.get("leverage_manager", {})
    tiers = [tuple(t) for t in lm_cfg.get("tiers", [
        (0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50),
    ])]
    engine = LiquidationEngine(
        maintenance_margin_ratio=lm_cfg.get("maintenance_margin_ratio", 0.25),
        buffer_start_ratio=lm_cfg.get("buffer_start_ratio", 0.45),
        tiers=tiers,
    )

    pos = Position(ticker="DEMO", size=1000, entry_price=100.0,
                   leverage=5.0, mark_price=100.0, is_long=True)

    print("Simulating a 5x leveraged long position as price declines...\n")
    print(f"{'Mark Price':>10} | {'Equity Ratio':>13} | {'Position Size':>13} | Action")
    print("-" * 70)

    for mark_price in [100, 97, 94, 91, 88.4, 86.8, 85.6, 82, 80]:
        pos.mark_price = float(mark_price)
        plan = engine.evaluate(pos)
        action_desc = "-"
        for act in plan.actions:
            pos.size -= act.shares_to_close
            action_desc = f"CLOSE {act.shares_to_close:.1f} sh ({act.close_fraction*100:.0f}%)"
        print(f"{mark_price:>10.2f} | {plan.equity_ratio:>13.3f} | "
              f"{pos.size:>13.1f} | {action_desc}")
        if plan.fully_liquidated:
            print("\nPosition fully liquidated.")
            break

    return 0


def run_live(ticker: str, interval: str, provider: str, host: str, port: int,
             poll_seconds: float, cfg: dict) -> int:
    from .live_feed import SimulatedLiveFeed, PollingFeed, AlpacaWebSocketFeed, generate_synthetic_backfill
    from .live_state import LiveWaveState
    from .dashboard import create_app

    pivot_cfg = cfg.get("pivot_detection", {})
    rules_cfg = cfg.get("wave_rules", {})
    alert_cfg = cfg.get("alerts", {})
    lm_cfg = cfg.get("leverage_manager", {})
    pt_cfg = cfg.get("paper_trading", {})
    leverage_tiers = [tuple(t) for t in lm_cfg.get("tiers", [
        (0.45, 0.10), (0.38, 0.15), (0.31, 0.25), (0.25, 0.50),
    ])]

    initial_df = None
    if provider in ("yfinance", "alpaca"):
        try:
            source = get_data_source("yfinance")  # used for chart backfill either way
            initial_df = source.get_ohlcv(ticker, interval, lookback=150)
            print(f"Backfilled {len(initial_df)} historical bars for {ticker}.")
        except Exception as e:
            print(f"Backfill failed ({e}); starting with an empty chart.")

    # Resolve the feed (and effective_provider, which can differ from the
    # requested `provider` on fallback) BEFORE constructing LiveWaveState,
    # so the very first snapshot the dashboard serves already reports the
    # correct source — not a placeholder that only becomes accurate after
    # the first live bar triggers a recompute.
    if provider == "simulated":
        start_price = float(initial_df["close"].iloc[-1]) if initial_df is not None and len(initial_df) else 100.0
        # Instantly backfill synthetic history (same model as the live feed)
        # so the chart and wave count are populated right away, the same
        # way the yfinance/Alpaca paths backfill real history — rather than
        # waiting for enough live bars to accumulate organically, which is
        # both slow and (with no fixed seed) unpredictably slow.
        initial_df = generate_synthetic_backfill(start_price=start_price, num_bars=60,
                                                   volatility_pct=1.5, bar_seconds=2.0)
        start_price = float(initial_df["close"].iloc[-1])
        feed = SimulatedLiveFeed(start_price=start_price, interval_seconds=2.0)
        effective_provider = "simulated"
        print("Using the simulated feed — synthetic bars, no API key or network needed.")
    elif provider == "yfinance":
        source = get_data_source("yfinance")
        feed = PollingFeed(source, ticker, interval=interval, poll_seconds=poll_seconds)
        effective_provider = "yfinance"
        print(f"Polling Yahoo Finance every {poll_seconds}s "
              "(typically ~15-20min delayed for intraday data).")
    elif provider == "alpaca":
        # Accept both Alpaca's own official env var names (APCA_API_KEY_ID /
        # APCA_API_SECRET_KEY, used by their SDK and docs) and the ALPACA_*
        # names this project's .env.example uses — so whichever convention
        # ends up in .env, it's actually picked up instead of silently
        # falling back to simulated with no clear reason why.
        api_key = os.environ.get("ALPACA_API_KEY_ID") or os.environ.get("APCA_API_KEY_ID", "")
        api_secret = os.environ.get("ALPACA_API_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")
        if not api_key or not api_secret:
            print("No Alpaca API keys found (checked ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY "
                  "and APCA_API_KEY_ID/APCA_API_SECRET_KEY). Copy .env.example to .env and "
                  "fill them in, or export them. Falling back to the simulated feed.")
            start_price = float(initial_df["close"].iloc[-1]) if initial_df is not None and len(initial_df) else 100.0
            feed = SimulatedLiveFeed(start_price=start_price, interval_seconds=2.0)
            effective_provider = "simulated (alpaca keys missing)"
        else:
            feed = AlpacaWebSocketFeed(ticker, api_key, api_secret)
            effective_provider = "alpaca"
            print("Connecting to Alpaca's real-time IEX bars stream "
                  "(free-plan single-venue data).")
    else:
        print(f"Unknown provider '{provider}'")
        return 1

    state = LiveWaveState(
        ticker=ticker,
        provider=effective_provider,
        zigzag_threshold_pct=pivot_cfg.get("zigzag_threshold_pct", 4.0),
        wave4_overlap_tolerance_pct=rules_cfg.get("wave4_overlap_tolerance_pct", 0.0),
        min_wave3_extension=rules_cfg.get("min_wave3_extension", 1.0),
        invalidation_buffer_pct=alert_cfg.get("invalidation_buffer_pct", 0.5),
        sudden_move_atr_multiple=alert_cfg.get("sudden_move_atr_multiple", 3.0),
        initial_df=initial_df,
        paper_starting_cash=pt_cfg.get("starting_cash", 100_000.0),
        maintenance_margin_ratio=lm_cfg.get("maintenance_margin_ratio", 0.25),
        buffer_start_ratio=lm_cfg.get("buffer_start_ratio", 0.45),
        leverage_tiers=leverage_tiers,
    )
    feed.start(state.add_bar)

    app = create_app(state)
    print(f"\nDashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        feed.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IMPULSEIQ — Elliott Wave analysis engine")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--lookback", type=int, default=250)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--demo-leverage", action="store_true",
                         help="Run the dynamic buffer liquidation demo instead of wave analysis")
    parser.add_argument("--live", action="store_true",
                         help="Launch the live dashboard instead of a one-shot analysis")
    parser.add_argument("--provider", default="simulated",
                         choices=["simulated", "yfinance", "alpaca"],
                         help="Live data provider (only used with --live)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--poll-seconds", type=float, default=30.0,
                         help="Poll interval for the yfinance live provider")
    args = parser.parse_args()

    cfg = load_config(args.config)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed — fine if ALPACA_* were exported directly

    if args.demo_leverage:
        return run_leverage_demo(cfg)
    if args.live:
        return run_live(args.ticker, args.interval, args.provider,
                         args.host, args.port, args.poll_seconds, cfg)
    return run_analysis(args.ticker, args.interval, args.lookback, cfg)


if __name__ == "__main__":
    sys.exit(main())
