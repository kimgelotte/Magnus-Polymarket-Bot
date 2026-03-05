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

## 4. Snabb öppning för att få fler köp

I `.env`:

```bash
MAGNUS_ALLOW_RETRADE_AFTER_DAYS=30
MAGNUS_REQUIRE_BELOW_AVG=0
```

Kör om, vänta tills en scannerrunda är klar (se rad med "→ Bouncer" och "skip: …") och följ med `tail_magnus.py` – då ser ni om det är REJECT, Skipping buy eller No edge som dominerar.
