"""
Live dashboard: a small Flask app that serves a candlestick chart (via
lightweight-charts, loaded from a CDN — this is a project you run locally,
not something published through Claude, so no CDN restrictions apply),
polls `LiveWaveState` for JSON updates, and exposes paper-trading order
endpoints. No real money or real orders are ever involved.

Run via `python -m src.main --live ...` rather than importing this directly.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from .live_state import LiveWaveState


def create_app(state: LiveWaveState) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html", ticker=state.ticker)

    @app.route("/api/state")
    def api_state():
        return jsonify(state.snapshot())

    @app.route("/api/order", methods=["POST"])
    def api_order():
        body = request.get_json(force=True, silent=True) or {}
        side = body.get("side")
        try:
            notional_usd = float(body.get("notional_usd", 0))
            leverage = float(body.get("leverage", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "notional_usd and leverage must be numbers"}), 400

        result = state.place_order(side, notional_usd, leverage)
        return jsonify({"ok": result.ok, "message": result.message}), (200 if result.ok else 400)

    @app.route("/api/close", methods=["POST"])
    def api_close():
        result = state.close_position()
        return jsonify({"ok": result.ok, "message": result.message}), (200 if result.ok else 400)

    return app
