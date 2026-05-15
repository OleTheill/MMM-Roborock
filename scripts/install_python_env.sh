#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$MODULE_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo
echo "Python environment ready in: $MODULE_DIR/venv"
echo "Next run: ./venv/bin/python scripts/setup_roborock.py --email your-roborock-email@example.com"
