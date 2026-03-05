"""
Magnus CLI – huvudentrépunkt.

Vanligaste kommandot:

    python -m scripts.python.cli run-autonomous-trader
"""

import argparse

from agents.application.trade import Trade


def run_autonomous_trader() -> None:
    """Startar Magnus V4 Sniper‑loopen."""
    trade = Trade()
    trade.run_sniper_loop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="magnus", description="Magnus Polymarket Sniper CLI")
    parser.add_argument(
        "command",
        nargs="?",
        default="run-autonomous-trader",
        help="Vilket kommando som ska köras (default: run-autonomous-trader)",
    )
    args = parser.parse_args()

    cmd = args.command
    try:
        if cmd == "run-autonomous-trader":
            run_autonomous_trader()
        else:
            raise SystemExit(f"Okänt kommando: {cmd}")
    except KeyboardInterrupt:
        # Snyggt avbrott vid Ctrl+C utan full traceback.
        print("\n👋 Magnus avbruten med Ctrl+C – stänger ner.")


if __name__ == "__main__":
    main()

