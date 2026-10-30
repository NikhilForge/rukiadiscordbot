#!/usr/bin/env bash
# Linux/macOS launcher: creates a venv if missing, installs requirements, runs Rukia.
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
python bot.py
