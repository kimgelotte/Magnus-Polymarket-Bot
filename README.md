## Magnus – Polymarket Sniper Bot

Magnus är en autonom trading‑agent för Polymarket.  
Han handlar inte “up or down”, utan jagar **prisrörelser**: köpa billigt, sälja dyrare innan resolution.

### Struktur

- `agents/application/trade.py` – huvudloopen (Sniper Mode, orderläggning, risk på trade‑nivå).
- `agents/application/scanner.py` – scanner‑tråd som hittar kandidater och matar kön.
- `agents/war_room.py` – “War Room”: Bouncer, Lawyer, Scout, Quant (AI‑beslutslogik).
- `agents/polymarket/polymarket.py` – Polymarket‑klient (CLOB + Gamma API).
- `agents/db_manager.py` – SQLite‑logg av trades och analyser.
- `agents/risk_manager.py` – Kelly‑baserad sizing av bets.
- `agents/portfolio_risk.py` – drawdown‑kontroll och enkel korrelationskontroll.
- `agents/dynamic_target.py` – beräknar dynamiskt target‑pris för GTC‑sell.
- `agents/observer.py` – enkel observer‑tråd för öppna positioner.
- `scripts/python/cli.py` – CLI‑entrypoint (`python -m scripts.python.cli run-autonomous-trader`).
- `scripts/revoke_polymarket_keys.py` – hjälpskript för att rotera Polymarket USER‑API‑nyckel via L1.
- `.env` – lokala hemligheter (privatnyckel, API‑nycklar, inställningar). **Ska aldrig in i git.**

### Krav

- Python 3.12 (virtuel miljö i `venv/` rekommenderas).
- Node/npm **endast** för hjälpskript (t.ex. typescript‑verktyg) – inte för själva bottens runtime.

Installera Python‑beroenden (om inte redan gjort):

```bash
cd /agents
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # om/ när requirements finns
```

### Miljövariabler (`.env`)

Alla hemliga värden hålls **endast** i `.env` (som är ignorerad av `.gitignore`).

För att komma igång:

1. Kopiera mallen:

   ```bash
   cp .env.example .env
   ```

2. Fyll i dina egna värden i `.env`:
   - `PRIVATE_KEY` – privata nyckeln för bot‑walleten på Polygon.
   - `POLYMARKET_FUNDER_ADDRESS` – adressen som håller USDC.e (samma som i Polymarket UI).
   - `POLYGON_CONFIG_MAINNET_RPC_URL` – din egen Alchemy/Infura‑URL.
   - AI‑nycklar: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, `TAVILY_API_KEY`, `NEWSAPI_API_KEY`.

3. Skapa CLOB USER‑API‑nycklar (L2) via SDK:

   ```bash
   # från projektroten, med PRIVATE_KEY satt i .env
   python -m scripts.python.create_polymarket_api_creds
   ```

   Skriptet skriver ut:

   ```json
   {
     "USER_API_KEY": "...",
     "USER_API_SECRET": "...",
     "USER_API_PASSPHRASE": "..."
   }
   ```

   Kopiera dessa värden in i `.env`:

   ```env
   USER_API_KEY=...
   USER_API_SECRET=...
   USER_API_PASSPHRASE=...
   ```

### Köra Magnus (Sniper Mode)

```bash
cd /agents
source venv/bin/activate
python3 -m scripts.python.cli run-autonomous-trader
```

### Säkerhet

- Rotera alltid **privata nycklar** och API‑nycklar vid läckor.
- Låt aldrig `.env` eller privata nycklar hamna i git eller på GitHub.
- Använd en dedikerad bot‑wallet (inte din personliga huvudwallet) för Magnus.