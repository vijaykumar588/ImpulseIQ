#!/usr/bin/env python3
"""
One-click launcher for IMPULSEIQ.

Double-click ImpulseIQ.bat (Windows) — or just run `python launch.py`
directly on any OS — and this:
  1. installs any missing dependencies (first run only)
  2. asks which ticker / data source to use
  3. starts the live dashboard server
  4. opens it in your default browser automatically

No manual `pip install`, no `python -m src.main ...` typing required.
"""

import importlib.util
import os
import subprocess
import sys
import time
import webbrowser

REQUIRED_MODULES = [
    "pandas", "numpy", "yfinance", "scipy", "yaml",
    "flask", "requests", "websockets", "dotenv",
]
# Import name -> actual pip package name, where they differ.
PIP_NAME = {"yaml": "pyyaml", "dotenv": "python-dotenv"}

DASHBOARD_URL = "http://127.0.0.1:8000"


def ensure_dependencies() -> None:
    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    if not missing:
        return
    pkgs = [PIP_NAME.get(m, m) for m in missing]
    print(f"First run: installing {', '.join(pkgs)} ...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])
    except subprocess.CalledProcessError:
        print("\n[ERROR] Automatic dependency install failed.")
        print("Try running this manually, then re-launch:")
        print(f"    {sys.executable} -m pip install -r requirements.txt")
        input("\nPress Enter to exit...")
        sys.exit(1)


def ask(prompt: str, default: str) -> str:
    try:
        value = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        value = ""
    return value or default


def main() -> None:
    # Run from this file's own folder, regardless of where it was launched
    # from or how the zip was extracted (avoids the classic "nested folder"
    # confusion: this always finds the real project root).
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 50)
    print("  IMPULSEIQ — Elliott Wave Trading Dashboard")
    print("=" * 50)
    print()

    ensure_dependencies()

    ticker = ask("Stock/crypto ticker to analyze", "AAPL")
    print()
    print("Data source:")
    print("  1) Simulated demo data   — works instantly, no setup")
    print("  2) Yahoo Finance         — real data, ~15-20 min delayed")
    print("  3) Alpaca real-time      — needs free API keys in .env")
    choice = ask("Choose 1, 2, or 3", "1")
    provider = {"2": "yfinance", "3": "alpaca"}.get(choice, "simulated")

    print(f"\nStarting IMPULSEIQ for {ticker} ({provider} data)...")
    server = subprocess.Popen([
        sys.executable, "-m", "src.main", "--live",
        "--ticker", ticker, "--provider", provider,
    ])

    # Give the Flask server a moment to bind before opening the browser.
    time.sleep(3)
    try:
        webbrowser.open(DASHBOARD_URL)
    except Exception:
        pass

    print(f"\nDashboard should now be open at {DASHBOARD_URL}")
    print("If your browser didn't open automatically, go there manually.")
    print("\nImpulseIQ is running. Close this window (or press Ctrl+C) to stop it.\n")

    try:
        server.wait()
    except KeyboardInterrupt:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    main()
