#!/usr/bin/env bash
set -e

echo "=== Magnus V4 Setup ==="

# 1. Python version check
python3 --version 2>/dev/null || { echo "Error: Python 3 is required."; exit 1; }

# 2. Virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "Using: $(python --version) at $(which python)"

# 3. Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 4. Environment file
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from template."
    echo "IMPORTANT: Edit .env and add your API keys before running the bot."
else
    echo ".env already exists — skipping."
fi

# 5. Data directory
mkdir -p data

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. Run: source .venv/bin/activate"
echo "  3. Run: python scripts/python/cli.py"
