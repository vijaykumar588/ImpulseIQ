#!/bin/bash
# Double-clickable launcher for macOS/Linux (macOS: rename extension handling
# may require "chmod +x ImpulseIQ.command" once; Linux: mark executable too).
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
    echo "Python 3 was not found on this system."
    echo "Install it from https://python.org/downloads and re-run this file."
    read -p "Press Enter to exit..."
    exit 1
fi

python3 launch.py
read -p "Press Enter to close this window..."
