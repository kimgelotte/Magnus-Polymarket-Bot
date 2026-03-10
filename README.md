## Magnus – Polymarket Sniper Bot

Magnus is an autonomous trading agent for Polymarket.  
It does not trade "up or down", but hunts **price movements**: buy cheap, sell dear before resolution.

### Structure

- `agents/application/trade.py` – main loop (Sniper Mode, order placement, trade-level risk).
- `agents/application/scanner.py` – scanner thread that finds candidates and feeds the queue.
- `agents/war_room.py` – "War Room": Bouncer, Lawyer, Scout, Quant (AI decision logic).
- `agents/polymarket/polymarket.py` – Polymarket client (CLOB + Gamma API).
- `agents/db_manager.py` – SQLite log of trades and analyses.
- `agents/risk_manager.py` – Kelly-based bet sizing.
- `agents/portfolio_risk.py` – drawdown control and simple correlation check.
- `agents/dynamic_target.py` – computes dynamic target price for GTC sell.
- `agents/observer.py` – simple observer thread for open positions.
- `scripts/python/cli.py` – CLI entrypoint (`python -m scripts.python.cli run-autonomous-trader`).
- `scripts/revoke_polymarket_keys.py` – helper script to rotate Polymarket USER API key via L1.
- `.env` – local secrets (private key, API keys, settings). **NEVER commit** – use `.env.example` as template.

### Requirements

- Python 3.12 (virtual env in `venv/` recommended).
- Node/npm **only** for helper scripts (e.g. TypeScript tools) – not for the bot runtime.

Install Python dependencies (if not already done):

```bash
cd /agents
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # if/when requirements exist
```

### Environment variables (`.env`)

All secret values are kept **only** in `.env` (which is ignored by `.gitignore`).

To get started:

1. Copy the template:

   ```bash
   cp .env.example .env
   ```

2. Fill in your own values in `.env`:
   - `PRIVATE_KEY` – private key for the bot wallet on Polygon.
   - `POLYMARKET_FUNDER_ADDRESS` – address that holds USDC.e (same as in Polymarket UI).
   - `POLYGON_CONFIG_MAINNET_RPC_URL` – your own Alchemy/Infura URL.
   - AI keys: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, `TAVILY_API_KEY`, `NEWSAPI_API_KEY`.

3. Create CLOB USER API keys (L2) via SDK:

   ```bash
   # from project root, with PRIVATE_KEY set in .env
   python -m scripts.python.create_polymarket_api_creds
   ```

   The script outputs:

   ```json
   {
     "USER_API_KEY": "...",
     "USER_API_SECRET": "...",
     "USER_API_PASSPHRASE": "..."
   }
   ```

   Copy these values into `.env`:

   ```env
   USER_API_KEY=...
   USER_API_SECRET=...
   USER_API_PASSPHRASE=...
   ```

### Running Magnus (Sniper Mode)

```bash
cd /agents
source venv/bin/activate
python3 -m scripts.python.cli run-autonomous-trader
```

### Security

- **`.env`** is in `.gitignore` – never commit this file.
- Always rotate **private keys** and API keys on leaks.
- Never let `.env` or private keys end up in git or on GitHub.
- Use a dedicated bot wallet (not your personal main wallet) for Magnus.
