# Varför hittar Magnus inget att köpa?

Flödet är: **Scanner (pre-filters)** → **Kö** → **War Room (Lawyer, Scout, Quant)** → **Post-checks** → **Köp**.

## 1. Titta i loggen

Kör `python scripts/python/tail_magnus.py` (eller titta i `magnus_live.log`). Nu loggas:

- **❌ REJECT: …** – War Room sa nej (Lawyer eller Quant). Orsaken står i texten.
- **⏸️ Skipping buy: price at/near average** – Quant sa BUY men vi kräver pris under snitt/lower half.
- **⚠️ No edge (Price: X / AI Max: Y)** – Quant sa BUY men edge (AI Max − pris) var för liten.

## 2. Titta på scanner-sammanfattningen

När en scannerrunda är klar skrivs t.ex.:

`📋 [Scanner] trending: 0 → Bouncer. | skip: scan=4500, liq=1200, days=800, price=200`

- **scan** = marknader vi redan äger eller redan har handlat (och inte tillåter återhandel).
- **liq** = för låg budlikviditet (&lt; 10 USDC).
- **days** = för lite tid kvar (&lt; min_days).
- **price** = pris utanför 0.10–0.80.
- **range** = för låg historisk volatilitet.
- **spread** = spread (bid–ask) över gräns – dessa skickas inte till War Room. Gräns: `MAGNUS_MAX_SPREAD_PCT` (default 35). Om du ser **0 → Bouncer** och **spread=50** eller liknande kan du prova `MAGNUS_MAX_SPREAD_PCT=40` så fler får chans; sänk till 25 om du bara vill ha tightare marknader.
- **dup** = redan i dedup (samma marknad skickad till kön inom senaste 5 min) – då läggs den inte in igen.
- **full** = kön är full (500 platser) – konsumenten hinner inte ta tillräckligt många, eller så har en tidigare runda fyllt kön.

Om du ser **"46 PASS, 0 i kön (dup=46)"** betyder det att alla 46 som passerade Bouncer redan räknades som duplicat (samma events går igen var 25:e sekund; dedup TTL är 300 s). **Lösning:** vänta tills konsumenten tömt kön och dedup har gått ut (5 min), eller sänk `dedup_ttl_seconds` i `MarketScanner` (t.ex. 60) om du vill att samma marknad snabbare får komma in igen.

Om du ser **"0 i kön (full=…)"** är kön full. **Lösning:** öka `WAR_ROOM_BATCH_SIZE` i `trade.py` (t.ex. 4–6) så fler kandidater plockas per konsumentrunda, eller starta om så kön börjar tom.

Om **scan** dominerar har ni redan handlat många marknader och de återanvänds inte.

## 3. Vad du kan göra (.env)

| Problem | Lösning |
|--------|--------|
| Nästan alla bortfiltreras av **scan** (redan handlad) | `MAGNUS_ALLOW_RETRADE_AFTER_DAYS=30` (eller 14) så marknader vi handlade för 30+ dagar sedan får komma in igen. |
| Många **REJECT** från Lawyer/Quant | Bouncer är redan av i scannern. Överväg `MAGNUS_SKIP_LAWYER=1` så bara Scout + Quant kör (sparar tokens, fler når Quant). |
| Många **Skipping buy: price at/near average** | `MAGNUS_REQUIRE_BELOW_AVG=0` – då kräver vi inte längre att pris ska vara under snitt/lower half (fler BUY blir faktiska köp). |
| Många **No edge** | Quant ger för låg MAX_PRICE. Kan testa sänka `min_edge_to_enter` i kod eller acceptera att många BUY inte har tillräcklig edge. |
| För få når Bouncer pga **liq** | Sänk: `MAGNUS_MIN_BID_LIQUIDITY=5`. |
| För få når Bouncer pga **days** | Kravet är redan mjukt (0.4 dag för Sport/Politics). Om ni vill ännu mjukare kan min_days sänkas i `scanner.py`. |
| Många **REJECT pga "Extremely high spread"** | Scannern filtrerar bort marknader med spread &gt; `MAGNUS_MAX_SPREAD_PCT` (default 35%). Sänk till 25 om du bara vill ha tightare marknader; höj till 40 om **0 → Bouncer** och du vill ge fler chans. |
| **0 → Bouncer** och **price=300+** dominerar | Prisbandet är 5¢–85¢ (i kod 0.05–0.85). På Polymarket är 0/100 avgjorda marknader; 1–99¢ är oddset (1 andel per cent). Vi undviker 0/1 och handlar i 0.05–0.85. Env: `MAGNUS_MIN_ENTRY_PRICE`, `MAGNUS_MAX_ENTRY_PRICE`. |

## 4. Snabb öppning för att få fler köp

I `.env`:

```bash
MAGNUS_ALLOW_RETRADE_AFTER_DAYS=30
MAGNUS_REQUIRE_BELOW_AVG=0
```

Kör om, vänta tills en scannerrunda är klar (se rad med "→ Bouncer" och "skip: …") och följ med `tail_magnus.py` – då ser ni om det är REJECT, Skipping buy eller No edge som dominerar.
