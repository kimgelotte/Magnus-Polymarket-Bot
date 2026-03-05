# API- och CPU-hotspots (Magnus V4)

Kartläggning för refaktorering: var gör vi repetitiva anrop och var kan vi cacha eller slå ihop.

## Scanner (`agents/application/scanner.py`)

| Plats | Anrop | Problem |
|-------|--------|--------|
| `_build_event_markets_overview` | `get_buy_price(token_id)` + `get_book(token_id)` per token i varje event | 2 CLOB-anrop per token |
| Samma event, inner loop (per market/token) | `get_buy_price`, `get_book`, `get_price_history` | Samma token kan redan ha hämtats i overview – dubbla anrop. `get_price_history` anropar dessutom `get_buy_price` internt (polymarket.py) |
| Rekommendation | Bygg candidate med pris/book från overview när samma (market, token) finns där; annars hämta. Inför caching i Polymarket för price/book/history per token (TTL några sekunder). |

## Trade (`agents/application/trade.py`)

| Plats | Anrop | Problem |
|-------|--------|--------|
| `manage_active_trades` | Per position: `get_token_balance(t_id)` → anropar `client.get_positions()` (hela listan) | N positioner = N anrop till get_positions(). Bättre: anropa get_positions() en gång, plocka ut balance per token_id i minnet. |
| `manage_active_trades` | `get_book(t_id)` och vid behov `get_buy_price(t_id)` per position | OK att ha per position, men om vi redan har positions-data kan vi undvika extra get_buy_price när vi har bid/ask från get_book. |
| Shadow mode i manage_active_trades | `get_price_history(t_id)` per position | Kan cachas (samma token kan inte ändras dramatiskt på några sekunder). |
| `run_batch` vid BUY | `get_buy_price(token_id)` (price_now) före order | Ytterligare ett anrop per köpkandidat. |
| `run_batch` efter order | `get_token_balance(token_id)` upp till 3 gånger i loop | Samma som ovan – get_positions() anropas flera gånger. Cacha positions eller anropa en gång och vänta. |

## Polymarket (`agents/polymarket/polymarket.py`)

| Metod | Beroenden | Kommentar |
|-------|-----------|-----------|
| `get_buy_price` | `client.get_price(token_id, side="BUY")` | Ingen timeout i vår kod (CLOB-klienten kan ha egen). Ingen caching. |
| `get_book` | `client.get_order_book(token_id)` | Ingen caching. |
| `get_price_history` | Anropar `get_buy_price(token_id)` och bygger syntetisk lista | Dubbel kostnad om någon redan anropat get_buy_price för samma token. |
| `get_token_balance` | `client.get_positions()` och itererar | Hämtar alla positioner varje gång – mycket anrop om det används ofta. |

## War Room

| Plats | Anrop | Kommentar |
|-------|--------|-----------|
| `evaluate_market` | Bouncer, Lawyer, Scout, Quant (LLM) | Batchning redan via asyncio.gather för flera kandidater. skip_bouncer/skip_lawyer minskar anrop. Rate limit / insufficient balance hanteras idag för DeepSeek; bör utvidgas till Grok/Claude. |

## Sammanfattning prioriteringar

1. **Cacha `get_positions()`** – anropa en gång per “runda” i manage_active_trades och vid behov i run_batch; exponera get_token_balance som tar optional positions-list.
2. **Cacha price/book/history i Polymarket** – enkel in-memory cache med kort TTL (t.ex. 30–60 s) per token_id för get_buy_price, get_book, get_price_history.
3. **Scanner: återanvänd overview-data** – för token som redan finns i event_markets_overview, använd redan hämtad price/bid/ask/spread istället för att hämta igen i inner loop.
4. **Timeout** – säkerställ att alla externa anrop (Gamma, RPC, CLOB) har rimlig timeout så att CPU inte blockar länge vid nätverksproblem.

## Risk verification (efter optimering)

Efter refaktorering av köp/sälj-flödet och mer köpvillig logik (t.ex. preferred categories) är följande oförändrat och aktivt:

| Komponent | Env / default | Användning |
|-----------|----------------|------------|
| **PortfolioRiskManager** | `MAGNUS_MAX_DRAWDOWN_PCT` (default 30) | Varje snipersväng: `check_drawdown(balance)` – pausar nya trades 5 min om drawdown ≥ limit. |
| | `MAGNUS_MAX_CORRELATED` (default 3) | Innan köp: `check_correlation(full_title, e_category)` – blockar om för många liknande positioner i samma kategori. |
| **Trade** | `max_open_positions` 15 (10 vid `MAGNUS_UNCERTAIN_MARKET=1`) | Innan köp: `open_count >= self.max_open_positions` → skippa. |
| **RiskManager** | Kelly-fraction, min_edge_to_enter, stop_loss_pct | Bet-storlek, ingångs-edge och stop-loss används som tidigare. |

Inga ändringar i risk-defaults krävdes; de ger fortfarande rimlig begränsning vid den mer aggressiva köplogiken.
