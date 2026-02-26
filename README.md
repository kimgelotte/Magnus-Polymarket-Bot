# Magnus V4 — Autonomous Polymarket Sniper Bot

Magnus is an autonomous trading bot for [Polymarket](https://polymarket.com) prediction markets. It continuously scans live markets, runs multi-agent AI analysis (Grok, Claude, DeepSeek), and executes trades on the Polygon blockchain — all without human intervention.

The strategy: **buy low, sell high**. Magnus hunts for edges in volatile markets, avoids noise, and exits positions before resolution.

## Architecture

```
Scanner Thread                        Main Thread
─────────────                        ───────────
Gamma API (trending)                  Consumer loop
  → Filter (price, time, liquidity)     ← Pull from queue
  → Bouncer / Gatekeeper (Grok)         → War Room (Lawyer + Scout + Quant)
  → Queue (bounded, 500)                → Execute buy via CLOB
                                        → Manage active trades (stop-loss, time-exit)
                                        → Observer (WebSocket price tracking)
```

### AI Agents (War Room)

| Agent | Model | Role |
|-------|-------|------|
| **Bouncer** | Grok | Time-horizon gatekeeper — enough time to sell at profit? |
| **Lawyer** | Claude | Rule analysis — clear resolution criteria? |
| **Scout** | Grok + Tavily/NewsAPI | Sentiment and live research — is the price likely to move up? |
| **Quant** | DeepSeek | Final decision — BUY/REJECT with max price and Kelly sizing |

### Risk Management

- **Kelly Criterion** position sizing with category-adjusted fractions
- **Stop-loss** at 20% with configurable arming delay (default 2h)
- **Time-based exit** when < 2 days remain and no profit
- **Liquidity filter** — minimum bid-side depth required before entry
- **Event-level limits** — max 1 position per balanced event (Sports), max 3 for multi-market events
- **Shadow mode** — experimental recovery-potential heuristic (logging only)

## Project Structure

```
├── agents/                        # Core production code
│   ├── application/
│   │   ├── trade.py               # Sniper loop, consumer, buy/sell/exit logic
│   │   └── scanner.py             # Producer thread — scans markets, runs Bouncer
│   ├── polymarket/
│   │   └── polymarket.py          # Blockchain + CLOB API (Web3, orders, book)
│   ├── war_room.py                # AI agents (Grok, Claude, DeepSeek)
│   ├── db_manager.py              # SQLite trade/analysis logging
│   ├── risk_manager.py            # Kelly Criterion bet sizing
│   ├── observer.py                # WebSocket real-time price observer
│   └── exceptions.py              # Custom exceptions
├── scripts/python/                # Utilities and tools
│   ├── cli.py                     # CLI entry point
│   ├── build_trades_chroma.py     # Build ChromaDB from trade history
│   ├── tail_magnus.py             # Live log tail utility
│   ├── restore_sell_orders.py     # Re-place GTC sell orders
│   ├── backfill_category.py       # Backfill categories in DB
│   ├── close_all_trades.py        # Emergency: close all positions
│   └── mark_trade_sold.py         # Manually mark a trade as sold
├── setup.sh                       # One-command setup (venv + deps + .env)
├── .env.example                   # Environment variable template
├── requirements.txt               # Python dependencies
├── README.md
└── LICENSE.md
```

## Setup

### Prerequisites

- Python 3.11+
- A Polygon wallet with USDC
- Polymarket CLOB API credentials
- API keys for at least: XAI (Grok), Anthropic (Claude), DeepSeek

### Quick Start

```bash
git clone https://github.com/kimgelotte/Magnus-Polymarket-Bot.git
cd Magnus-Polymarket-Bot
bash setup.sh
```

This creates a virtual environment, installs dependencies, and copies `.env.example` to `.env`.

### Configuration

Edit `.env` and add your API keys:

Required variables:

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Polygon wallet private key |
| `POLYMARKET_FUNDER_ADDRESS` | Your proxy/funder address |
| `USER_API_KEY` / `USER_SECRET` / `USER_PASSPHRASE` | CLOB API credentials |
| `XAI_API_KEY` | Grok API key |
| `ANTHROPIC_API_KEY` | Claude API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |

Optional:

| Variable | Default | Description |
|----------|---------|-------------|
| `TAVILY_API_KEY` | — | Web research for Scout agent |
| `NEWSAPI_API_KEY` | — | News research for Scout agent |
| `MAGNUS_UNCERTAIN_MARKET` | `0` | Set `1` for defensive mode (lower bets, stricter filters) |
| `MAGNUS_MIN_BID_LIQUIDITY` | `20.0` | Min bid-side liquidity (USDC) to allow entry |
| `MAGNUS_MIN_HOLD_HOURS` | `2.0` | Hours before stop-loss can activate |

### Running

```bash
source .venv/bin/activate

# Start the sniper bot
python scripts/python/cli.py

# In another terminal — watch live activity
python scripts/python/tail_magnus.py
```

## Category Strategy

Magnus prioritizes categories based on liquidity, catalyst clarity, and edge predictability:

| Priority | Categories | Why |
|----------|-----------|-----|
| **Preferred** | Sports, Elections, Politics | High liquidity, clear catalysts, fast resolution |
| **Standard** | Pop Culture, Science, etc. | Normal filters and sizing |
| **High-risk** | Crypto, Geopolitics, Business, Tech, Economics | Stricter entry (mandatory lower-half price, higher edge, lower Kelly) |

## Security

- **Never** commit `.env` — it contains private keys and API secrets
- `.env` is in `.gitignore` by default
- All sensitive values are loaded via `os.getenv()` at runtime
- The bot operates on Polygon mainnet with real USDC — use at your own risk

## Disclaimer

This software is provided as-is for educational and research purposes. Trading on prediction markets involves real financial risk. The authors are not responsible for any losses incurred. Always review the code and understand the risks before running with real funds.

## License

[MIT](LICENSE.md)
